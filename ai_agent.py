"""Administrative patient-access agent. Business actions always execute through capabilities."""
import os, re, json, requests
import capabilities as cap

SPECIALTY_KEYWORDS = {
    "orthopedics": ["orthopedics", "orthopedic", "orthopaedics", "ortho", "shoulder", "knee", "joint", "bone", "fracture", "back pain"],
    "cardiology": ["cardiology", "cardiologist", "cardiac", "chest pain", "heart", "palpitation", "blood pressure"],
    "dermatology": ["dermatology", "dermatologist", "skin", "rash", "acne"],
    "general medicine": ["general medicine", "general physician", "general checkup", "checkup", "fever", "cold", "cough", "flu"],
    "ophthalmology": ["ophthalmology", "ophthalmologist", "eye", "vision"],
    "pediatrics": ["pediatrics", "paediatrics", "pediatric", "paediatric", "child", "baby", "kid"],
    "psychiatry": ["psychiatry", "psychiatrist", "anxiety", "stress", "depression", "mental health", "sleep"],
    "dentistry": ["dentistry", "dentist", "dental", "tooth", "teeth"],
}
CANCEL_KEYWORDS = ["cancel my appointment", "cancel it", "cancel appointment", "cancel"]
LOOKUP_KEYWORDS = ["show my appointment", "show my appointments", "my appointment", "my appointments", "upcoming appointment", "upcoming appointments", "do i have an appointment", "check my appointment", "appointment status", "appointment details"]
RESCHEDULE_KEYWORDS = ["reschedule", "change my appointment", "move my appointment", "change the appointment"]
QUESTIONNAIRE_KEYWORDS = ["questionnaire", "pre-visit", "pre visit", "complete my questions", "complete the questionnaire", "fill questionnaire", "fill out questionnaire"]


def _extract_specialty(text):
    t=text.lower()
    for spec,kws in SPECIALTY_KEYWORDS.items():
        if any(k in t for k in kws): return spec
    return None

def _detect_admin_intent(text):
    t=text.lower()
    if any(k in t for k in CANCEL_KEYWORDS): return "cancel"
    if any(k in t for k in RESCHEDULE_KEYWORDS): return "reschedule"
    if any(k in t for k in LOOKUP_KEYWORDS): return "lookup"
    if any(k in t for k in QUESTIONNAIRE_KEYWORDS): return "questionnaire"
    return None

def _most_recent_active_appointment(conn, actor):
    row=conn.execute("SELECT id FROM appointments WHERE patient_id=? AND status NOT IN ('cancelled','completed','no_show') ORDER BY created_at DESC LIMIT 1",(actor.get("patient_id"),)).fetchone()
    return row["id"] if row else None

def _extract_index_choice(text,n):
    t=text.lower(); nums={"first":1,"second":2,"third":3,"fourth":4,"fifth":5,"sixth":6,"seventh":7,"eighth":8}
    for w,num in nums.items():
        if re.search(rf"\b{w}\b",t) and num<=n: return num-1
    m=re.search(r"\b(?:option|number|slot|doctor)?\s*(\d+)\b",t)
    if m:
        i=int(m.group(1))-1
        if 0<=i<n:return i
    return None

def _llm_extract(message,context):
    key=os.environ.get("ANTHROPIC_API_KEY")
    if not key:return None
    try:
        prompt=("You are an administrative healthcare intent extractor. Never diagnose, prescribe, recommend treatment, "
                "or make clinical assessments. Return JSON only with specialty, action, choice_index. action must be one of "
                "search, select_doctor, select_slot, cancel, reschedule, questionnaire, lookup, other. "
                f"Context:{json.dumps(context)} Message:{message}")
        r=requests.post("https://api.anthropic.com/v1/messages",headers={"x-api-key":key,"anthropic-version":"2023-06-01","content-type":"application/json"},json={"model":"claude-sonnet-4-6","max_tokens":200,"messages":[{"role":"user","content":prompt}]},timeout=8)
        r.raise_for_status(); txt=r.json()["content"][0]["text"].strip().strip("```json").strip("```").strip(); return json.loads(txt)
    except Exception:return None

def _doctor_details(d):
    return (f"Dr. {d['name']}\nSpecialty: {d.get('specialty') or 'Not specified'}\n"
            f"Department: {d.get('department') or 'Not specified'}\nHospital: {d.get('hospital_name','Not specified')}\n"
            f"Consultation duration: {d.get('duration_minutes',30)} minutes")

def _appointment_text(conn,appt):
    row=conn.execute("""SELECT a.*,d.name doctor_name,d.specialty,h.name hospital_name,h.address hospital_address
                       FROM appointments a JOIN doctors d ON d.id=a.doctor_id JOIN hospitals h ON h.id=a.hospital_id WHERE a.id=?""",(appt['id'],)).fetchone()
    a=dict(row)
    return (f"Your appointment:\nDr. {a['doctor_name']} ({a['specialty']})\n{a['hospital_name']}\n"
            f"{a['slot_date']} at {a['start_time']}\nStatus: {a['status']}\nAppointment ID: {a['id']}")

def _questionnaire_start(conn,actor,context,appt_id):
    q=cap.get_questionnaire(conn,actor,conn.execute("SELECT hospital_id FROM appointments WHERE id=?",(appt_id,)).fetchone()['hospital_id'])
    if not q['success']: return q['error']['message'],context
    questions=q['data']['questions']; context={**context,"questionnaire":{"appointment_id":appt_id,"questionnaire_id":q['data']['id'],"questions":questions,"answers":{},"index":0}}
    if not questions:return "There are no questionnaire questions configured.",context
    return f"Your pre-visit questionnaire is ready. {questions[0]['label']}",context

def handle_message(conn,actor,context,message):
    context=dict(context or {}); intent=_llm_extract(message,context); fallback=intent is None
    detected=(intent or {}).get('action') if intent else _detect_admin_intent(message)

    # Conversational questionnaire state has highest priority.
    qs=context.get('questionnaire')
    if qs:
        i=qs['index']; qs['answers'][qs['questions'][i]['id']]=message.strip(); qs['index']+=1
        if qs['index']<len(qs['questions']):
            return qs['questions'][qs['index']]['label'],context,fallback
        result=cap.submit_questionnaire(conn,actor,qs['appointment_id'],qs['answers'])
        context.pop('questionnaire',None)
        return (("Your pre-visit questionnaire has been submitted and is available to the doctor." if result['success'] else f"I couldn't submit the questionnaire: {result['error']['message']}"),context,fallback)

    appt_id=context.get('last_appointment_id') or _most_recent_active_appointment(conn,actor)
    if detected=="lookup":
        if not appt_id:return "I don't see any appointment on file for you. Would you like to book one?",context,fallback
        res=cap.get_appointment(conn,actor,appt_id)
        if not res['success']:return f"I couldn't retrieve that appointment: {res['error']['message']}",context,fallback
        return _appointment_text(conn,res['data']),{**context,"last_appointment_id":appt_id},fallback
    if detected=="cancel":
        if not appt_id:return "I don't see any active appointment to cancel.",context,fallback
        res=cap.cancel_appointment(conn,actor,appt_id,reason="patient requested via chat")
        return (("Your appointment has been cancelled and the hospital's system has been updated." if res['success'] else f"I couldn't cancel that appointment: {res['error']['message']}"),({} if res['success'] else context),fallback)
    if detected=="questionnaire":
        if not appt_id:return "You do not have an active appointment with a questionnaire yet.",context,fallback
        reply,newctx=_questionnaire_start(conn,actor,context,appt_id); return reply,newctx,fallback
    if detected=="reschedule":
        if not appt_id:return "I don't see an active appointment to reschedule. Would you like to book one?",context,fallback
        ap=cap.get_appointment(conn,actor,appt_id)
        if not ap['success']:return f"I couldn't retrieve that appointment: {ap['error']['message']}",context,fallback
        slots=cap.check_availability(conn,actor,ap['data']['doctor_id'])['data']
        slots=[s for s in slots if not (s['date']==ap['data']['slot_date'] and s['start_time']==ap['data']['start_time'])][:8]
        if not slots:return "I couldn't find another available slot for this doctor in the next 7 days.",context,fallback
        context={**context,"last_appointment_id":appt_id,"reschedule_slots":slots}
        lines=[f"{i+1}. {s['date']} at {s['start_time']}" for i,s in enumerate(slots)]
        return "I found these alternative slots:\n"+"\n".join(lines)+"\nWhich slot would you like?",context,fallback

    if context.get('reschedule_slots'):
        idx=(intent or {}).get('choice_index') if intent else _extract_index_choice(message,len(context['reschedule_slots']))
        if idx is None:return "Please choose an alternative slot by number.",context,fallback
        s=context['reschedule_slots'][idx]; res=cap.reschedule_appointment(conn,actor,context['last_appointment_id'],s['date'],s['start_time'])
        if not res['success']:return f"I couldn't reschedule that appointment: {res['error']['message']}",context,fallback
        context={"last_appointment_id":context['last_appointment_id']}
        a=res['data']['appointment']; return f"Your appointment has been rescheduled to {a['slot_date']} at {a['start_time']} and the hospital's system has been verified.",context,fallback

    # doctor discovery
    specialty=(intent or {}).get('specialty') if intent else _extract_specialty(message)
    if specialty and not context.get('doctors'):
        res=cap.search_doctors(conn,actor,specialty=specialty); doctors=res['data'] if res['success'] else []
        if not doctors:return f"I couldn't find active doctors for '{specialty}'. Could you give me another administrative concern or specialty?",context,fallback
        context.update(specialty=specialty,doctors=doctors)
        lines=[f"{i+1}. Dr. {d['name']} ({d['specialty']}) - {d['hospital_name']}" for i,d in enumerate(doctors)]
        return "I found these doctors who may help (administrative routing only, not a diagnosis):\n"+"\n".join(lines)+"\nWhich doctor would you like, or say 'more info'?",context,fallback

    if context.get('doctors') and not context.get('selected_doctor') and message.strip().lower() in {'yes','yes please','show appointments','show available appointments'}:
        if len(context['doctors']) != 1:
            return "Please choose a doctor by number first.",context,fallback
        idx=0
        doctor=context['doctors'][idx]; slots=cap.check_availability(conn,actor,doctor['id'])['data']
        if not slots:return f"Dr. {doctor['name']} has no open slots in the next 7 days. Try another doctor?",context,fallback
        context['selected_doctor']=doctor; context['slots']=slots[:8]
        return f"Here are real available slots for Dr. {doctor['name']}:\n"+"\n".join(f"{i+1}. {s['date']} at {s['start_time']}" for i,s in enumerate(context['slots']))+"\nWhich slot works for you?",context,fallback

    if context.get('doctors') and not context.get('selected_doctor'):
        if any(x in message.lower() for x in ['more info','tell me more','details','information about']):
            idx=_extract_index_choice(message,len(context['doctors']))
            if idx is None and len(context['doctors']) != 1:
                return "Which doctor do you want more information about? Reply with the doctor number, for example 1.",context,fallback
            idx = 0 if idx is None else idx
            return _doctor_details(context['doctors'][idx])+"\nWould you like to see available appointments for this doctor?",context,fallback
        idx=(intent or {}).get('choice_index') if intent else _extract_index_choice(message,len(context['doctors']))
        if idx is None:
            for i,d in enumerate(context['doctors']):
                if d['name'].lower() in message.lower():idx=i;break
        if idx is None:return "Please pick a doctor by number or say 'more info'.",context,fallback
        doctor=context['doctors'][idx]; slots=cap.check_availability(conn,actor,doctor['id'])['data']
        if not slots:return f"Dr. {doctor['name']} has no open slots in the next 7 days. Try another doctor?",context,fallback
        context['selected_doctor']=doctor; context['slots']=slots[:8]
        return f"Here are real available slots for Dr. {doctor['name']}:\n"+"\n".join(f"{i+1}. {s['date']} at {s['start_time']}" for i,s in enumerate(context['slots']))+"\nWhich slot works for you?",context,fallback

    if context.get('slots') and context.get('selected_doctor'):
        idx=(intent or {}).get('choice_index') if intent else _extract_index_choice(message,len(context['slots']))
        if idx is None:return "Please pick a slot by number from the list above.",context,fallback
        s=context['slots'][idx]; d=context['selected_doctor']; r=cap.create_appointment(conn,actor,d['id'],s['date'],s['start_time'])
        if not r['success']:return f"Sorry, I couldn't complete that booking: {r['error']['message']}. Please choose another slot.",context,fallback
        a=r['data']['appointment']; newctx={'last_appointment_id':a['id']}
        if a['status']=='confirmed':reply=f"Your appointment with Dr. {d['name']} on {a['slot_date']} at {a['start_time']} is booked and verified with the hospital's system. A pre-visit questionnaire and reminder have been set up for you."
        else:reply=f"Your booking is being processed (status: {a['status']}). We'll confirm shortly."
        return reply,newctx,fallback

    return ("Hi! Tell me your administrative need (for example, 'shoulder pain this week') and I can help find a doctor, check availability, book, reschedule, cancel, or retrieve an appointment. I cannot diagnose or give medical advice."),context,fallback
