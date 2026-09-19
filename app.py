import os, json, functools
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from dotenv import load_dotenv

load_dotenv()
from db import get_conn, init_db, DB_PATH
import capabilities as cap
import ai_agent
from utils import hash_pw

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.jinja_env.filters["from_json"] = json.loads

# Apply the schema on every startup so new prototype tables are added to an existing DB.
init_db()
if not os.path.exists(DB_PATH):
    import seed
    seed.run()
else:
    conn = get_conn()
    has_hospitals = conn.execute("SELECT COUNT(*) c FROM hospitals").fetchone()["c"] > 0
    conn.close()
    if not has_hospitals:
        import seed
        seed.run()

# ---------------- auth helpers ----------------

def current_actor():
    if "user_id" not in session:
        return None
    return {"user_id": session["user_id"], "email": session["email"], "role": session["role"],
            "hospital_id": session.get("hospital_id"), "patient_id": session.get("patient_id"),
            "doctor_id": session.get("doctor_id"), "name": session.get("name")}

def login_required(*roles):
    def deco(f):
        @functools.wraps(f)
        def wrapper(*a, **kw):
            actor = current_actor()
            if not actor:
                return redirect(url_for("login"))
            if roles and actor["role"] not in roles:
                return "Forbidden: role not authorized for this page", 403
            return f(actor, *a, **kw)
        return wrapper
    return deco

ROLE_HOME_ENDPOINT = {
    "patient": "patient_home",
    "doctor": "doctor_home",
    "hospital_admin": "hospital_home",
    "platform_admin": "platform_home",
}

@app.route("/")
def index():
    actor = current_actor()
    if not actor:
        return redirect(url_for("login"))
    return redirect(url_for(ROLE_HOME_ENDPOINT[actor["role"]]))

@app.route("/hospital/register", methods=["GET", "POST"])
def hospital_register():
    if request.method == "POST":
        name=request.form.get("name","").strip(); address=request.form.get("address","").strip(); admin_name=request.form.get("admin_name","").strip(); email=request.form.get("email","").strip().lower(); password=request.form.get("password","")
        if not all([name,admin_name,email,password]): flash("Hospital name, administrator name, email and password are required"); return redirect(url_for("hospital_register"))
        conn=get_conn()
        if conn.execute("SELECT 1 FROM users WHERE email=?",(email,)).fetchone(): conn.close(); flash("That administrator email is already registered"); return redirect(url_for("hospital_register"))
        hid=conn.execute("INSERT INTO hospitals(name,address,status) VALUES (?,?, 'submitted')",(name,address)).lastrowid
        conn.execute("INSERT INTO hospital_settings(hospital_id) VALUES (?)",(hid,))
        conn.execute("INSERT INTO users(email,password,role,hospital_id,name) VALUES (?,?,?,?,?)",(email,hash_pw(password),"hospital_admin",hid,admin_name))
        conn.commit(); conn.close(); flash("Hospital application submitted. A platform administrator must approve it before booking is enabled."); return redirect(url_for("login"))
    return render_template("hospital_register.html", actor=None)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        pw = request.form["password"]
        conn = get_conn()
        u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not u or u["password"] != hash_pw(pw):
            flash("Invalid credentials"); return redirect(url_for("login"))
        session["user_id"] = u["id"]; session["email"] = u["email"]; session["role"] = u["role"]
        session["hospital_id"] = u["hospital_id"]; session["name"] = u["name"]
        session["patient_id"] = None; session["doctor_id"] = None
        if u["role"] == "patient":
            p = conn.execute("SELECT id FROM patients WHERE user_id=?", (u["id"],)).fetchone()
            session["patient_id"] = p["id"] if p else None
        if u["role"] == "doctor":
            d = conn.execute("SELECT id FROM doctors WHERE user_id=?", (u["id"],)).fetchone()
            session["doctor_id"] = d["id"] if d else None
        conn.close()
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        phone = request.form.get("phone", "").strip()
        dob = request.form.get("dob", "").strip()
        if not name or not email or not password:
            flash("Name, email and password are required"); return redirect(url_for("register"))
        conn = get_conn()
        existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            conn.close()
            flash("An account with that email already exists. Please log in instead.")
            return redirect(url_for("login"))
        cur = conn.execute("INSERT INTO users(email,password,role,name) VALUES (?,?, 'patient', ?)",
                            (email, hash_pw(password), name))
        user_id = cur.lastrowid
        conn.execute("INSERT INTO patients(user_id,name,dob,phone) VALUES (?,?,?,?)",
                     (user_id, name, dob or None, phone or None))
        conn.commit()
        patient_id = conn.execute("SELECT id FROM patients WHERE user_id=?", (user_id,)).fetchone()["id"]
        conn.close()
        # log the new patient straight in
        session["user_id"] = user_id; session["email"] = email; session["role"] = "patient"
        session["hospital_id"] = None; session["name"] = name
        session["patient_id"] = patient_id; session["doctor_id"] = None
        return redirect(url_for("index"))
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------------- Patient ----------------

@app.route("/patient")
@login_required("patient")
def patient_home(actor):
    conn = get_conn()
    appts = conn.execute("""SELECT a.*, d.name as doctor_name, h.name as hospital_name FROM appointments a
                             JOIN doctors d ON d.id=a.doctor_id JOIN hospitals h ON h.id=a.hospital_id
                             WHERE a.patient_id=? ORDER BY a.slot_date DESC""", (actor["patient_id"],)).fetchall()
    conn.close()
    return render_template("patient_home.html", actor=actor, appts=appts)

@app.route("/patient/ai")
@login_required("patient")
def patient_ai(actor):
    return render_template("patient_ai.html", actor=actor)

@app.route("/api/chat", methods=["POST"])
@login_required("patient")
def api_chat(actor):
    message = request.json.get("message", "")
    simulate_timeout = bool(request.json.get("simulate_timeout"))
    context = session.get("ai_context", {})
    conn = get_conn()
    if simulate_timeout and context.get("slots") and context.get("selected_doctor"):
        # honor the visible failure-demo control mid-conversation
        from ai_agent import _extract_index_choice
        idx = _extract_index_choice(message, len(context["slots"])) or 0
        slot = context["slots"][idx]; doctor = context["selected_doctor"]
        result = cap.create_appointment(conn, actor, doctor["id"], slot["date"], slot["start_time"], simulate_ehr_timeout=True)
        session["ai_context"] = {}
        conn.close()
        appt = result["data"]["appointment"] if result["success"] else None
        reply = "Simulated EHR timeout triggered. See the operation trace below for the recovery outcome."
        return jsonify({"reply": reply, "fallback": False, "op_trace": result.get("data")})
    reply, new_context, used_fallback = ai_agent.handle_message(conn, actor, context, message)
    session["ai_context"] = new_context
    conn.close()
    return jsonify({"reply": reply, "fallback": used_fallback})

@app.route("/patient/questionnaire/<int:appt_id>", methods=["GET", "POST"])
@login_required("patient")
def patient_questionnaire(actor, appt_id):
    conn = get_conn()
    appt = conn.execute("SELECT * FROM appointments WHERE id=? AND patient_id=?", (appt_id, actor["patient_id"])).fetchone()
    if not appt:
        conn.close(); return "Appointment not found", 404
    q_res = cap.get_questionnaire(conn, actor, appt["hospital_id"])
    if request.method == "POST":
        answers = {k: v for k, v in request.form.items()}
        result = cap.submit_questionnaire(conn, actor, appt_id, answers)
        conn.close()
        if not result["success"]: return result["error"]["message"], 400
        flash("Questionnaire submitted"); return redirect(url_for("patient_home"))
    conn.close()
    if not q_res["success"]: return q_res["error"]["message"], 404
    return render_template("questionnaire.html", actor=actor, appt=appt, q=q_res["data"])

@app.route("/patient/appointment/<int:appt_id>/reschedule", methods=["GET", "POST"])
@login_required("patient")
def patient_reschedule(actor, appt_id):
    conn=get_conn(); ap=cap.get_appointment(conn, actor, appt_id)
    if not ap["success"]: conn.close(); return ap["error"]["message"], 404
    slots=cap.check_availability(conn, actor, ap["data"]["doctor_id"])["data"]
    slots=[x for x in slots if not (x["date"]==ap["data"]["slot_date"] and x["start_time"]==ap["data"]["start_time"])][:12]
    if request.method=="POST":
        idx=int(request.form["slot_index"]); chosen=slots[idx]
        result=cap.reschedule_appointment(conn,actor,appt_id,chosen["date"],chosen["start_time"])
        conn.close()
        if not result["success"]: flash(result["error"]["message"]); return redirect(url_for("patient_home"))
        flash("Appointment rescheduled and verified with the hospital system."); return redirect(url_for("patient_home"))
    conn.close(); return render_template("reschedule.html", actor=actor, appt=ap["data"], slots=slots)

@app.route("/patient/preferences", methods=["GET", "POST"])
@login_required("patient")
def patient_preferences(actor):
    conn=get_conn()
    if request.method=="POST":
        conn.execute("UPDATE patients SET name=?, phone=?, dob=? WHERE id=?", (request.form.get("name", actor["name"]).strip(), request.form.get("phone",""), request.form.get("dob") or None, actor["patient_id"]))
        session["name"] = request.form.get("name", actor["name"]).strip()
        conn.execute("INSERT INTO patient_preferences(patient_id,preferred_time,communication_method,notes) VALUES (?,?,?,?) ON CONFLICT(patient_id) DO UPDATE SET preferred_time=excluded.preferred_time,communication_method=excluded.communication_method,notes=excluded.notes,updated_at=CURRENT_TIMESTAMP",(actor["patient_id"],request.form.get("preferred_time"),request.form.get("communication_method"),request.form.get("notes")))
        conn.commit(); flash("Preferences updated")
    pref=conn.execute("SELECT * FROM patient_preferences WHERE patient_id=?",(actor["patient_id"],)).fetchone()
    patient=conn.execute("SELECT * FROM patients WHERE id=?",(actor["patient_id"],)).fetchone(); conn.close()
    return render_template("patient_preferences.html", actor=actor, patient=patient, pref=pref)

@app.route("/patient/appointment/<int:appt_id>/cancel", methods=["POST"])
@login_required("patient")
def patient_cancel(actor, appt_id):
    conn = get_conn(); cap.cancel_appointment(conn, actor, appt_id, reason="patient requested"); conn.close()
    return redirect(url_for("patient_home"))

# ---------------- Doctor ----------------

@app.route("/doctor")
@login_required("doctor")
def doctor_home(actor):
    conn = get_conn()
    appts = conn.execute("""SELECT a.*, p.name as patient_name FROM appointments a JOIN patients p ON p.id=a.patient_id
                             WHERE a.doctor_id=? AND a.status NOT IN ('cancelled') ORDER BY a.slot_date""",
                          (actor["doctor_id"],)).fetchall()
    conn.close()
    return render_template("doctor_home.html", actor=actor, appts=appts)

@app.route("/doctor/appointment/<int:appt_id>")
@login_required("doctor")
def doctor_appt(actor, appt_id):
    conn = get_conn()
    res = cap.get_appointment(conn, actor, appt_id)
    if not res["success"]:
        conn.close(); return res["error"]["message"], 403
    responses = conn.execute("""SELECT qr.*, q.name as q_name, q.questions_json FROM questionnaire_responses qr
                                 JOIN questionnaires q ON q.id=qr.questionnaire_id WHERE qr.appointment_id=?""",
                              (appt_id,)).fetchall()
    conn.close()
    return render_template("appointment_detail.html", actor=actor, appt=res["data"], responses=responses)

@app.route("/hospital/onboarding", methods=["GET", "POST"])
@login_required("platform_admin")
def hospital_onboarding(actor):
    if request.method=="POST":
        conn=get_conn(); cur=conn.execute("INSERT INTO hospitals(name,address,status) VALUES (?,?, 'submitted')",(request.form["name"].strip(),request.form.get("address","")))
        hid=cur.lastrowid; conn.execute("INSERT OR IGNORE INTO hospital_settings(hospital_id) VALUES (?)",(hid,)); conn.commit(); conn.close()
        flash("Hospital application submitted for review."); return redirect(url_for("platform_home"))
    return render_template("hospital_onboarding.html", actor=actor)

@app.route("/hospital/settings", methods=["GET", "POST"])
@login_required("hospital_admin")
def hospital_settings(actor):
    conn=get_conn(); h=conn.execute("SELECT * FROM hospitals WHERE id=?",(actor["hospital_id"],)).fetchone()
    if request.method=="POST":
        settings=(json.dumps([x.strip() for x in request.form.get("departments","").split(",") if x.strip()]),json.dumps([x.strip() for x in request.form.get("specialties","").split(",") if x.strip()]),json.dumps([x.strip() for x in request.form.get("appointment_types","").split(",") if x.strip()]),json.dumps({"email":bool(request.form.get("email")),"sms":bool(request.form.get("sms"))}),request.form.get("integration_name") or "Mock EHR")
        conn.execute("INSERT INTO hospital_settings(hospital_id,departments_json,specialties_json,appointment_types_json,communication_preferences_json,integration_name) VALUES (?,?,?,?,?,?) ON CONFLICT(hospital_id) DO UPDATE SET departments_json=excluded.departments_json,specialties_json=excluded.specialties_json,appointment_types_json=excluded.appointment_types_json,communication_preferences_json=excluded.communication_preferences_json,integration_name=excluded.integration_name,updated_at=CURRENT_TIMESTAMP",(actor["hospital_id"],*settings)); conn.commit(); flash("Hospital configuration saved")
    cfg=conn.execute("SELECT * FROM hospital_settings WHERE hospital_id=?",(actor["hospital_id"],)).fetchone()
    conn.close(); return render_template("hospital_settings.html",actor=actor,hospital=h,settings=cfg)

@app.route("/doctor/availability", methods=["GET", "POST"])
@login_required("doctor")
def doctor_availability(actor):
    conn=get_conn(); doctor=conn.execute("SELECT * FROM doctors WHERE id=?",(actor["doctor_id"],)).fetchone()
    if request.method=="POST":
        if request.form.get("action")=="block":
            conn.execute("INSERT INTO blocked_slots(doctor_id,date,start_time,end_time,reason) VALUES (?,?,?,?,?)",(actor["doctor_id"],request.form["date"],request.form["start_time"],request.form["end_time"],request.form.get("reason","")))
        else:
            conn.execute("INSERT INTO availability(doctor_id,weekday,start_time,end_time) VALUES (?,?,?,?)",(actor["doctor_id"],int(request.form["weekday"]),request.form["start_time"],request.form["end_time"]))
        conn.commit()
    avail=conn.execute("SELECT * FROM availability WHERE doctor_id=? ORDER BY weekday,start_time",(actor["doctor_id"],)).fetchall(); blocked=conn.execute("SELECT * FROM blocked_slots WHERE doctor_id=? ORDER BY date,start_time",(actor["doctor_id"],)).fetchall(); conn.close()
    return render_template("doctor_self_availability.html",actor=actor,doctor=doctor,avail=avail,blocked=blocked)

# ---------------- Hospital Admin ----------------

@app.route("/hospital")
@login_required("hospital_admin")
def hospital_home(actor):
    conn = get_conn()
    doctors = conn.execute("SELECT * FROM doctors WHERE hospital_id=?", (actor["hospital_id"],)).fetchall()
    appts = conn.execute("""SELECT a.*, d.name as doctor_name, p.name as patient_name FROM appointments a
                             JOIN doctors d ON d.id=a.doctor_id JOIN patients p ON p.id=a.patient_id
                             WHERE a.hospital_id=? ORDER BY a.created_at DESC LIMIT 30""", (actor["hospital_id"],)).fetchall()
    ops = conn.execute("""SELECT eo.* FROM ehr_operations eo JOIN appointments a ON a.id=eo.appointment_id
                           WHERE a.hospital_id=? ORDER BY eo.created_at DESC LIMIT 20""", (actor["hospital_id"],)).fetchall()
    workflows = conn.execute("""SELECT w.* FROM workflows w JOIN appointments a ON a.id=w.appointment_id
                                 WHERE a.hospital_id=? ORDER BY w.created_at DESC LIMIT 20""", (actor["hospital_id"],)).fetchall()
    hospital = conn.execute("SELECT * FROM hospitals WHERE id=?", (actor["hospital_id"],)).fetchone()
    conn.close()
    return render_template("hospital_admin.html", actor=actor, doctors=doctors, appts=appts, ops=ops,
                            workflows=workflows, hospital=hospital)

@app.route("/hospital/doctor/add", methods=["POST"])
@login_required("hospital_admin")
def hospital_add_doctor(actor):
    conn = get_conn()
    conn.execute("INSERT INTO doctors(hospital_id,name,specialty,department,duration_minutes,status) VALUES (?,?,?,?,?, 'active')",
                 (actor["hospital_id"], request.form["name"], request.form["specialty"], request.form.get("department", ""),
                  int(request.form.get("duration_minutes", 30))))
    conn.commit(); conn.close()
    return redirect(url_for("hospital_home"))

@app.route("/hospital/doctor/<int:doctor_id>/availability", methods=["GET", "POST"])
@login_required("hospital_admin")
def hospital_doctor_availability(actor, doctor_id):
    conn = get_conn()
    doctor = conn.execute("SELECT * FROM doctors WHERE id=? AND hospital_id=?", (doctor_id, actor["hospital_id"])).fetchone()
    if not doctor:
        conn.close(); return "Not found or not in your hospital", 404
    if request.method == "POST":
        conn.execute("INSERT INTO availability(doctor_id,weekday,start_time,end_time) VALUES (?,?,?,?)",
                     (doctor_id, int(request.form["weekday"]), request.form["start_time"], request.form["end_time"]))
        conn.commit()
    avail = conn.execute("SELECT * FROM availability WHERE doctor_id=?", (doctor_id,)).fetchall()
    blocked = conn.execute("SELECT * FROM blocked_slots WHERE doctor_id=?", (doctor_id,)).fetchall()
    conn.close()
    return render_template("doctor_availability.html", actor=actor, doctor=doctor, avail=avail, blocked=blocked)

@app.route("/hospital/doctor/<int:doctor_id>/block", methods=["POST"])
@login_required("hospital_admin")
def hospital_block_slot(actor, doctor_id):
    conn = get_conn()
    conn.execute("INSERT INTO blocked_slots(doctor_id,date,start_time,end_time,reason) VALUES (?,?,?,?,?)",
                 (doctor_id, request.form["date"], request.form["start_time"], request.form["end_time"],
                  request.form.get("reason", "")))
    conn.commit(); conn.close()
    return redirect(url_for("hospital_doctor_availability", doctor_id=doctor_id))

# ---------------- Platform Admin ----------------

@app.route("/platform")
@login_required("platform_admin")
def platform_home(actor):
    conn = get_conn()
    hospitals = conn.execute("SELECT * FROM hospitals").fetchall()
    recon = conn.execute("""SELECT r.*, a.hospital_id FROM reconciliation_records r
                             LEFT JOIN appointments a ON a.id=r.appointment_id ORDER BY r.created_at DESC LIMIT 30""").fetchall()
    audit = conn.execute("SELECT * FROM audit_events ORDER BY created_at DESC LIMIT 50").fetchall()
    metrics = {
        "hospitals": len(hospitals),
        "appointments": conn.execute("SELECT COUNT(*) c FROM appointments").fetchone()["c"],
        "confirmed": conn.execute("SELECT COUNT(*) c FROM appointments WHERE status='confirmed'").fetchone()["c"],
        "reconciliation_open": conn.execute("SELECT COUNT(*) c FROM reconciliation_records WHERE status!='resolved'").fetchone()["c"],
        "ehr_unknown": conn.execute("SELECT COUNT(*) c FROM ehr_operations WHERE status='unknown'").fetchone()["c"],
        "ai_actions": conn.execute("SELECT COUNT(*) c FROM audit_events WHERE action IN ('search_doctors','check_availability','get_appointment','create_appointment','reschedule_appointment','cancel_appointment')").fetchone()["c"],
        "workflow_completed": conn.execute("SELECT COUNT(*) c FROM workflows WHERE status='completed'").fetchone()["c"],
        "booking_failures": conn.execute("SELECT COUNT(*) c FROM audit_events WHERE action='create_appointment_failed'").fetchone()["c"],
    }
    conn.close()
    return render_template("platform_admin.html", actor=actor, hospitals=hospitals, recon=recon, audit=audit, metrics=metrics)

@app.route("/platform/hospital/<int:hospital_id>/status", methods=["POST"])
@login_required("platform_admin")
def platform_hospital_status(actor, hospital_id):
    conn = get_conn()
    conn.execute("UPDATE hospitals SET status=? WHERE id=?", (request.form["status"], hospital_id))
    conn.commit(); conn.close()
    return redirect(url_for("platform_home"))

@app.route("/platform/reconciliation/<int:rec_id>/resolve", methods=["POST"])
@login_required("platform_admin")
def platform_resolve(actor, rec_id):
    conn = get_conn()
    conn.execute("UPDATE reconciliation_records SET status='resolved' WHERE id=?", (rec_id,))
    conn.commit(); conn.close()
    return redirect(url_for("platform_home"))

# ---------------- Failure demo (shared) ----------------

@app.route("/demo/failure", methods=["GET", "POST"])
@login_required("patient")
def demo_failure(actor):
    conn = get_conn()
    doctors = cap.search_doctors(conn, actor)["data"]
    result = None
    if request.method == "POST":
        doctor_id = int(request.form["doctor_id"])
        slots = cap.check_availability(conn, actor, doctor_id)["data"]
        if slots:
            slot = slots[0]
            result = cap.create_appointment(conn, actor, doctor_id, slot["date"], slot["start_time"],
                                             simulate_ehr_timeout=True)
    conn.close()
    return render_template("demo_failure.html", actor=actor, doctors=doctors, result=result)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
