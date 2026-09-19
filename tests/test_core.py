import os, sys, json, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from db import get_conn, init_db
import seed, scheduling, capabilities as cap
from ehr import ehr_connector

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    import db
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    import capabilities, scheduling as sch  # noqa
    init_db(reset=True)
    seed.run()
    c = get_conn()
    yield c
    c.close()

def actor_for(conn, email):
    u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    a = {"user_id": u["id"], "email": u["email"], "role": u["role"], "hospital_id": u["hospital_id"]}
    if u["role"] == "patient":
        a["patient_id"] = conn.execute("SELECT id FROM patients WHERE user_id=?", (u["id"],)).fetchone()["id"]
    if u["role"] == "doctor":
        a["doctor_id"] = conn.execute("SELECT id FROM doctors WHERE user_id=?", (u["id"],)).fetchone()["id"]
    return a

def get_doctor_id(conn, name):
    return conn.execute("SELECT id FROM doctors WHERE name=?", (name,)).fetchone()["id"]

# 1. availability calculation & blocked/booked slots
def test_availability_respects_blocked_and_booked(conn):
    did = get_doctor_id(conn, "Ananya Rao")
    slots = scheduling.get_available_slots(conn, did)
    assert len(slots) > 0
    first = slots[0]
    conn.execute("INSERT INTO blocked_slots(doctor_id,date,start_time,end_time,reason) VALUES (?,?,?,?,?)",
                 (did, first["date"], first["start_time"], first["end_time"], "leave"))
    conn.commit()
    slots2 = scheduling.get_available_slots(conn, did)
    assert first not in slots2

# 2. double booking prevention
def test_double_booking_prevented(conn):
    did = get_doctor_id(conn, "Ananya Rao")
    slots = scheduling.get_available_slots(conn, did)
    s = slots[0]
    a1, err1 = scheduling.try_reserve_slot(conn, did, 1, 1, s["date"], s["start_time"], "idem-1")
    assert a1 is not None
    a2, err2 = scheduling.try_reserve_slot(conn, did, 1, 2, s["date"], s["start_time"], "idem-2")
    assert a2 is None and "booked" in err2.lower()

# 3. appointment state transitions
def test_appointment_confirms_after_booking(conn):
    actor = actor_for(conn, "meera@patient.dev")
    did = get_doctor_id(conn, "Ananya Rao")
    slots = cap.check_availability(conn, actor, did)["data"]
    r = cap.create_appointment(conn, actor, did, slots[0]["date"], slots[0]["start_time"])
    assert r["success"]
    assert r["data"]["appointment"]["status"] == "confirmed"

# 4. capability authorization/validation
def test_capability_rejects_wrong_role(conn):
    doctor_actor = actor_for(conn, "ananya@doctor.dev")
    did = get_doctor_id(conn, "Ananya Rao")
    slots = cap.check_availability(conn, doctor_actor, did)["data"]
    r = cap.create_appointment(conn, doctor_actor, did, slots[0]["date"], slots[0]["start_time"])
    assert not r["success"] and r["error"]["code"] == "FORBIDDEN"

# 5. tenant isolation
def test_tenant_isolation(conn):
    patient_actor = actor_for(conn, "meera@patient.dev")
    did = get_doctor_id(conn, "Ananya Rao")
    slots = cap.check_availability(conn, patient_actor, did)["data"]
    booked = cap.create_appointment(conn, patient_actor, did, slots[0]["date"], slots[0]["start_time"])["data"]["appointment"]
    other_hospital_admin = actor_for(conn, "admin@lakeview.dev")
    r = cap.get_appointment(conn, other_hospital_admin, booked["id"])
    assert not r["success"] and r["error"]["code"] == "FORBIDDEN"

# 6. idempotent booking
def test_idempotent_booking_returns_same_appointment(conn):
    actor = actor_for(conn, "meera@patient.dev")
    did = get_doctor_id(conn, "Ananya Rao")
    slots = cap.check_availability(conn, actor, did)["data"]
    key = "fixed-idem-key"
    r1 = cap.create_appointment(conn, actor, did, slots[0]["date"], slots[0]["start_time"], idempotency_key=key)
    r2 = cap.create_appointment(conn, actor, did, slots[0]["date"], slots[0]["start_time"], idempotency_key=key)
    assert r1["data"]["appointment"]["id"] == r2["data"]["appointment"]["id"]

# 7. EHR timeout after external creation + 8. verification/sync + 9. unknown outcome recovery without duplicate
def test_ehr_timeout_recovers_without_duplicate(conn):
    actor = actor_for(conn, "meera@patient.dev")
    did = get_doctor_id(conn, "Ananya Rao")
    slots = cap.check_availability(conn, actor, did)["data"]
    r = cap.create_appointment(conn, actor, did, slots[0]["date"], slots[0]["start_time"], simulate_ehr_timeout=True)
    assert r["success"]
    appt = r["data"]["appointment"]
    assert appt["status"] == "confirmed"  # recovered automatically since mock EHR record was found
    assert r["data"]["recovery"]["outcome"] == "found_and_synchronized"
    # exactly one external record should exist for this idempotency key - no duplicate
    count = conn.execute("SELECT COUNT(*) c FROM mock_ehr_appointments WHERE idempotency_key=?",
                          (appt["idempotency_key"],)).fetchone()["c"]
    assert count == 1

# 10. questionnaire/workflow trigger
def test_questionnaire_and_workflow_triggered_after_booking(conn):
    actor = actor_for(conn, "meera@patient.dev")
    did = get_doctor_id(conn, "Ananya Rao")
    slots = cap.check_availability(conn, actor, did)["data"]
    r = cap.create_appointment(conn, actor, did, slots[0]["date"], slots[0]["start_time"])
    appt_id = r["data"]["appointment"]["id"]
    wf_row = conn.execute("SELECT * FROM workflows WHERE appointment_id=?", (appt_id,)).fetchone()
    assert wf_row is not None and wf_row["status"] == "completed"
    qres = cap.submit_questionnaire(conn, actor, appt_id, {"reason": "shoulder pain follow-up"})
    assert qres["success"]

def test_reschedule_and_cancel(conn):
    actor = actor_for(conn, "meera@patient.dev")
    did = get_doctor_id(conn, "Ananya Rao")
    slots = cap.check_availability(conn, actor, did)["data"]
    appt = cap.create_appointment(conn, actor, did, slots[0]["date"], slots[0]["start_time"])["data"]["appointment"]
    new_slot = slots[1]
    r = cap.reschedule_appointment(conn, actor, appt["id"], new_slot["date"], new_slot["start_time"])
    assert r["success"] and r["data"]["appointment"]["status"] == "confirmed"
    r2 = cap.cancel_appointment(conn, actor, appt["id"])
    assert r2["success"] and r2["data"]["appointment"]["status"] == "cancelled"

# 11. regression: AI must recognize a specialty spoken by name, not only via symptom keywords
def test_ai_recognizes_specialty_spoken_directly():
    import ai_agent
    assert ai_agent._extract_specialty("General Medicine.") == "general medicine"
    assert ai_agent._extract_specialty("Cardiology") == "cardiology"
    assert ai_agent._extract_specialty("Psychiatry please") == "psychiatry"
    assert ai_agent._extract_specialty("Dentistry") == "dentistry"
    # symptom-based matching must still work
    assert ai_agent._extract_specialty("I have shoulder pain") == "orthopedics"

# 12. regression: chat must support "show my appointment" and "cancel my appointment",
# even after the booking flow has reset (this was previously a dead end)
def test_ai_chat_lookup_and_cancel(conn):
    import ai_agent
    actor = actor_for(conn, "meera@patient.dev")

    ctx = {}
    reply, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "shoulder pain")
    reply, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "1")
    reply, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "1")
    assert "booked and verified" in reply
    assert ctx.get("last_appointment_id")

    reply, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "Show my appointment.")
    assert "confirmed" in reply.lower()

    reply, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "I want to cancel my appointment.")
    assert "cancelled" in reply.lower()

    reply, ctx, _ = ai_agent.handle_message(conn, actor, {}, "Show my appointment.")
    assert "don't see any appointment" in reply.lower()

# 13. conversational rescheduling flow

def test_ai_conversational_reschedule(conn):
    import ai_agent
    actor = actor_for(conn, "meera@patient.dev")
    ctx = {}
    _, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "shoulder pain")
    _, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "1")
    reply, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "1")
    assert "booked and verified" in reply
    old_id = ctx["last_appointment_id"]
    reply, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "I want to reschedule my appointment")
    assert "alternative slots" in reply.lower()
    reply, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "1")
    assert "rescheduled" in reply.lower()
    assert conn.execute("SELECT status FROM appointments WHERE id=?", (old_id,)).fetchone()["status"] == "confirmed"

# 14. conversational questionnaire collection stores structured answers

def test_ai_conversational_questionnaire(conn):
    import ai_agent
    actor = actor_for(conn, "meera@patient.dev")
    did = get_doctor_id(conn, "Ananya Rao")
    slot = cap.check_availability(conn, actor, did)["data"][0]
    appt = cap.create_appointment(conn, actor, did, slot["date"], slot["start_time"])["data"]["appointment"]
    ctx = {"last_appointment_id": appt["id"]}
    reply, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "I want to complete my questionnaire")
    assert "first visit" in reply.lower() or "reason for visit" in reply.lower()
    while ctx.get("questionnaire"):
        reply, ctx, _ = ai_agent.handle_message(conn, actor, ctx, "Yes, this is my answer")
    assert "submitted" in reply.lower()
    row = conn.execute("SELECT * FROM questionnaire_responses WHERE appointment_id=?", (appt["id"],)).fetchone()
    assert row is not None

# 15. questionnaire route data is patient-owned

def test_questionnaire_capability_rejects_other_patient(conn):
    patient = actor_for(conn, "meera@patient.dev")
    other = actor_for(conn, "arjun@patient.dev")
    did = get_doctor_id(conn, "Ananya Rao")
    slot = cap.check_availability(conn, patient, did)["data"][0]
    appt = cap.create_appointment(conn, patient, did, slot["date"], slot["start_time"])["data"]["appointment"]
    result = cap.submit_questionnaire(conn, other, appt["id"], {"reason": "unauthorized"})
    assert not result["success"] and result["error"]["code"] == "FORBIDDEN"
