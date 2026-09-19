"""Controlled capability layer.
Every action the AI (or UI) performs goes through here: validated inputs,
authorization/tenant checks, structured results, audit events, idempotency.
The AI never touches the database or EHR directly - only these functions.
"""
import json
from utils import audit, new_id, ok, err
import scheduling
from ehr import ehr_connector, EHRTimeoutError
import workflows as wf

ACTIVE = scheduling.ACTIVE_STATUSES

# ---------- discovery ----------

def search_hospitals(conn, actor, query=None):
    rows = conn.execute("SELECT id,name,address,status FROM hospitals WHERE status='approved'").fetchall()
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r["name"].lower()]
    audit(conn, actor["email"], actor["role"], None, "search_hospitals", details={"query": query})
    conn.commit()
    return ok([dict(r) for r in rows])

def search_doctors(conn, actor, hospital_id=None, specialty=None, query=None):
    sql = """SELECT d.id,d.name,d.specialty,d.department,d.duration_minutes,d.hospital_id,h.name as hospital_name
             FROM doctors d JOIN hospitals h ON h.id=d.hospital_id
             WHERE d.status='active' AND h.status='approved'"""
    params = []
    if hospital_id:
        sql += " AND d.hospital_id=?"; params.append(hospital_id)
    if specialty:
        sql += " AND LOWER(d.specialty) LIKE ?"; params.append(f"%{specialty.lower()}%")
    rows = conn.execute(sql, params).fetchall()
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r["name"].lower() or q in (r["specialty"] or "").lower()]
    audit(conn, actor["email"], actor["role"], hospital_id, "search_doctors", details={"specialty": specialty, "query": query})
    conn.commit()
    return ok([dict(r) for r in rows])

def check_availability(conn, actor, doctor_id, days_ahead=7):
    doctor = conn.execute("SELECT * FROM doctors WHERE id=?", (doctor_id,)).fetchone()
    if not doctor:
        return err("NOT_FOUND", "Doctor not found")
    slots = scheduling.get_available_slots(conn, doctor_id, days_ahead=days_ahead)
    audit(conn, actor["email"], actor["role"], doctor["hospital_id"], "check_availability",
          "doctor", doctor_id, {"count": len(slots)})
    conn.commit()
    return ok(slots)

# ---------- patients / appointments read ----------

def lookup_patient(conn, actor, patient_id):
    patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if not patient:
        return err("NOT_FOUND", "Patient not found")
    if actor["role"] == "patient" and actor.get("patient_id") != patient_id:
        return err("FORBIDDEN", "Patients may only access their own record")
    if actor["role"] in ("doctor", "hospital_admin"):
        linked = conn.execute(
            "SELECT 1 FROM appointments WHERE patient_id=? AND hospital_id=?", (patient_id, actor["hospital_id"])
        ).fetchone()
        if not linked:
            return err("FORBIDDEN", "No authorized relationship with this patient")
    audit(conn, actor["email"], actor["role"], actor.get("hospital_id"), "lookup_patient", "patient", patient_id)
    conn.commit()
    return ok(dict(patient))

def _authorize_appointment_access(conn, actor, appt):
    if actor["role"] == "patient" and appt["patient_id"] != actor.get("patient_id"):
        return err("FORBIDDEN", "Patients may only access their own appointments")
    if actor["role"] == "doctor" and appt["doctor_id"] != actor.get("doctor_id"):
        return err("FORBIDDEN", "Doctors may only access their own appointments")
    if actor["role"] == "hospital_admin" and appt["hospital_id"] != actor.get("hospital_id"):
        return err("FORBIDDEN", "Hospital scoping violation")
    return None

def get_appointment(conn, actor, appointment_id):
    appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
    if not appt:
        return err("NOT_FOUND", "Appointment not found")
    appt = dict(appt)
    denial = _authorize_appointment_access(conn, actor, appt)
    if denial:
        return denial
    audit(conn, actor["email"], actor["role"], appt["hospital_id"], "get_appointment", "appointment", appointment_id)
    conn.commit()
    return ok(appt)

# ---------- booking sequence (create/reschedule/cancel) ----------

def create_appointment(conn, actor, doctor_id, date_s, start_time, idempotency_key=None, simulate_ehr_timeout=False):
    if actor["role"] != "patient" or not actor.get("patient_id"):
        return err("FORBIDDEN", "Only patients may book their own appointments")
    doctor = conn.execute("SELECT * FROM doctors WHERE id=?", (doctor_id,)).fetchone()
    if not doctor:
        return err("NOT_FOUND", "Doctor not found")
    hospital = conn.execute("SELECT * FROM hospitals WHERE id=?", (doctor["hospital_id"],)).fetchone()
    if doctor["status"] != "active" or hospital["status"] != "approved":
        return err("NOT_BOOKABLE", "Doctor inactive or hospital not approved")

    idempotency_key = idempotency_key or new_id("idem")
    patient_id = actor["patient_id"]

    appt, reason = scheduling.try_reserve_slot(conn, doctor_id, doctor["hospital_id"], patient_id, date_s, start_time, idempotency_key)
    if not appt:
        audit(conn, actor["email"], actor["role"], doctor["hospital_id"], "create_appointment_failed",
              "doctor", doctor_id, {"reason": reason})
        conn.commit()
        return err("SLOT_UNAVAILABLE", reason)

    operation_id = new_id("op")
    conn.execute("UPDATE appointments SET operation_id=? WHERE id=?", (operation_id, appt["id"]))
    conn.execute("INSERT INTO ehr_operations(operation_id,appointment_id,op_type,status,request_json) VALUES (?,?,?,?,?)",
                 (operation_id, appt["id"], "create_appointment", "started",
                  json.dumps({"date": date_s, "start_time": start_time, "idempotency_key": idempotency_key})))
    conn.commit()

    patient_ext = ehr_connector.lookup_or_create_patient(conn, patient_id)
    provider_ext = ehr_connector.lookup_or_create_provider(conn, doctor_id)

    result = {"appointment": None, "ehr_operation": operation_id, "verification": None,
              "recovery": None, "reconciliation": None}

    try:
        ehr_resp = ehr_connector.create_appointment(
            conn, idempotency_key=idempotency_key, patient_external_id=patient_ext,
            provider_external_id=provider_ext, date=date_s, start_time=start_time, end_time=appt["end_time"],
            simulate_timeout=simulate_ehr_timeout)
        conn.execute("UPDATE ehr_operations SET status='succeeded', response_json=?, updated_at=CURRENT_TIMESTAMP WHERE operation_id=?",
                     (json.dumps(ehr_resp), operation_id))
        verified = _verify_and_sync(conn, appt["id"], ehr_resp["external_id"])
        result["verification"] = verified
        _post_confirm(conn, appt["id"])
        conn.commit()
        result["appointment"] = dict(conn.execute("SELECT * FROM appointments WHERE id=?", (appt["id"],)).fetchone())
        audit(conn, actor["email"], actor["role"], doctor["hospital_id"], "create_appointment", "appointment", appt["id"],
              {"operation_id": operation_id, "outcome": "confirmed"})
        conn.commit()
        return ok(result)

    except EHRTimeoutError as e:
        # Outcome unknown: never blindly retry creation. Query EHR using the stable
        # idempotency key to discover the true final state.
        conn.execute("UPDATE ehr_operations SET status='unknown', response_json=?, updated_at=CURRENT_TIMESTAMP WHERE operation_id=?",
                     (json.dumps({"error": str(e)}), operation_id))
        conn.execute("UPDATE appointments SET status='sync_pending', updated_at=CURRENT_TIMESTAMP WHERE id=?", (appt["id"],))
        conn.commit()

        found = ehr_connector.get_appointment(conn, idempotency_key=idempotency_key)
        if found:
            verified = _verify_and_sync(conn, appt["id"], found["external_id"])
            result["recovery"] = {"outcome": "found_and_synchronized", "external_id": found["external_id"]}
            result["verification"] = verified
            conn.execute("UPDATE ehr_operations SET status='succeeded', updated_at=CURRENT_TIMESTAMP WHERE operation_id=?", (operation_id,))
            _post_confirm(conn, appt["id"])
            conn.commit()
            audit(conn, actor["email"], actor["role"], doctor["hospital_id"], "ehr_timeout_recovered",
                  "appointment", appt["id"], {"operation_id": operation_id})
        else:
            conn.execute("UPDATE appointments SET status='reconciliation_required', updated_at=CURRENT_TIMESTAMP WHERE id=?", (appt["id"],))
            conn.execute("INSERT INTO reconciliation_records(appointment_id,operation_id,status,details) VALUES (?,?,?,?)",
                         (appt["id"], operation_id, "open", "EHR timeout and record not found on query; human review required"))
            result["reconciliation"] = "record_not_found_escalated"
            transfer_to_human(conn, actor, appt["id"], "EHR unknown outcome could not be resolved automatically")
        conn.commit()
        result["appointment"] = dict(conn.execute("SELECT * FROM appointments WHERE id=?", (appt["id"],)).fetchone())
        return ok(result)

def _verify_and_sync(conn, appointment_id, external_id):
    """verify_external_appointment + synchronize_state, used internally by the booking sequence."""
    record = ehr_connector.get_appointment(conn, external_id=external_id)
    verified = bool(record and record["status"] == "booked")
    conn.execute("UPDATE appointments SET external_appointment_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (external_id, appointment_id))
    if verified:
        conn.execute("UPDATE appointments SET status='confirmed' WHERE id=?", (appointment_id,))
    conn.commit()
    return {"verified": verified, "external_record": record}

def _post_confirm(conn, appointment_id):
    appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
    q = conn.execute("SELECT * FROM questionnaires WHERE hospital_id=? AND status='approved' LIMIT 1",
                      (appt["hospital_id"],)).fetchone()
    patient_user = conn.execute("SELECT user_id FROM patients WHERE id=?", (appt["patient_id"],)).fetchone()
    doctor_user = conn.execute("SELECT user_id FROM doctors WHERE id=?", (appt["doctor_id"],)).fetchone()
    if q:
        wf.send_notification(conn, patient_user["user_id"], appointment_id, "questionnaire_assigned",
                              f"Please complete the pre-visit questionnaire: {q['name']}")
    wf.run_post_booking_workflow(conn, dict(appt), patient_user["user_id"] if patient_user else None,
                                  doctor_user["user_id"] if doctor_user else None)

def verify_external_appointment(conn, actor, appointment_id):
    appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
    if not appt:
        return err("NOT_FOUND", "Appointment not found")
    denial = _authorize_appointment_access(conn, actor, dict(appt))
    if denial:
        return denial
    if not appt["external_appointment_id"]:
        return err("NOT_LINKED", "No external record linked yet")
    record = ehr_connector.get_appointment(conn, external_id=appt["external_appointment_id"])
    audit(conn, actor["email"], actor["role"], appt["hospital_id"], "verify_external_appointment",
          "appointment", appointment_id, {"found": bool(record)})
    conn.commit()
    return ok({"verified": bool(record), "external_record": record})

def synchronize_state(conn, actor, appointment_id):
    appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
    if not appt:
        return err("NOT_FOUND", "Appointment not found")
    denial = _authorize_appointment_access(conn, actor, dict(appt))
    if denial:
        return denial
    if appt["external_appointment_id"]:
        result = _verify_and_sync(conn, appointment_id, appt["external_appointment_id"])
    else:
        found = ehr_connector.get_appointment(conn, idempotency_key=appt["idempotency_key"])
        if found:
            result = _verify_and_sync(conn, appointment_id, found["external_id"])
        else:
            result = {"verified": False, "external_record": None}
    audit(conn, actor["email"], actor["role"], appt["hospital_id"], "synchronize_state", "appointment", appointment_id, result)
    conn.commit()
    return ok(result)

def reschedule_appointment(conn, actor, appointment_id, new_date, new_start_time):
    """Reschedule with external verification before reporting success."""
    appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
    if not appt:
        return err("NOT_FOUND", "Appointment not found")
    appt = dict(appt)
    denial = _authorize_appointment_access(conn, actor, appt)
    if denial:
        return denial
    if appt["status"] not in ("confirmed", "requested", "pending", "rescheduled"):
        return err("INVALID_STATE", f"Cannot reschedule appointment in status {appt['status']}")
    bookable, info = scheduling.is_slot_bookable(conn, appt["doctor_id"], new_date, new_start_time)
    if not bookable:
        return err("SLOT_UNAVAILABLE", info)
    new_end = info

    # The old slot remains untouched until the external system confirms the move.
    verification = None
    if appt["external_appointment_id"]:
        try:
            record = ehr_connector.reschedule_appointment(
                conn, appt["external_appointment_id"], new_date, new_start_time, new_end
            )
        except Exception as exc:
            audit(conn, actor["email"], actor["role"], appt["hospital_id"],
                  "reschedule_appointment_failed", "appointment", appointment_id,
                  {"error": str(exc)})
            conn.commit()
            return err("EXTERNAL_UPDATE_FAILED", "The hospital system could not be updated. The original appointment was kept.")
        verification = {"verified": bool(record and record.get("status") == "booked"), "external_record": record}
        if not verification["verified"]:
            return err("EXTERNAL_VERIFICATION_FAILED", "The hospital system did not verify the new appointment time.")

    try:
        conn.execute("""UPDATE appointments SET slot_date=?, start_time=?, end_time=?, status='confirmed',
                      updated_at=CURRENT_TIMESTAMP WHERE id=?""", (new_date, new_start_time, new_end, appointment_id))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        # If a real connector were used, this would require a compensating action.
        return err("CONFLICT", str(exc))

    patient_user = conn.execute("SELECT user_id FROM patients WHERE id=?", (appt["patient_id"],)).fetchone()
    if patient_user:
        wf.send_notification(conn, patient_user["user_id"], appointment_id, "rescheduled",
                             f"Your appointment was moved to {new_date} at {new_start_time}.")
    audit(conn, actor["email"], actor["role"], appt["hospital_id"], "reschedule_appointment", "appointment", appointment_id,
          {"new_date": new_date, "new_start_time": new_start_time, "verified": True})
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone())
    return ok({"appointment": updated, "verification": verification})

def cancel_appointment(conn, actor, appointment_id, reason=None):
    appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
    if not appt:
        return err("NOT_FOUND", "Appointment not found")
    appt = dict(appt)
    denial = _authorize_appointment_access(conn, actor, appt)
    if denial:
        return denial
    if appt["status"] == "cancelled":
        return ok({"appointment": appt, "note": "already cancelled"})
    verification = None
    if appt["external_appointment_id"]:
        record = ehr_connector.cancel_appointment(conn, appt["external_appointment_id"])
        verification = {"verified": record and record["status"] == "cancelled", "external_record": record}
    conn.execute("UPDATE appointments SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE id=?", (appointment_id,))
    conn.commit()
    patient_user = conn.execute("SELECT user_id FROM patients WHERE id=?", (appt["patient_id"],)).fetchone()
    if patient_user:
        wf.send_notification(conn, patient_user["user_id"], appointment_id, "cancellation",
                              f"Your appointment on {appt['slot_date']} at {appt['start_time']} was cancelled.")
    audit(conn, actor["email"], actor["role"], appt["hospital_id"], "cancel_appointment", "appointment", appointment_id, {"reason": reason})
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone())
    return ok({"appointment": updated, "verification": verification})

# ---------- questionnaire ----------

def get_questionnaire(conn, actor, hospital_id):
    q = conn.execute("SELECT * FROM questionnaires WHERE hospital_id=? AND status='approved' LIMIT 1", (hospital_id,)).fetchone()
    if not q:
        return err("NOT_FOUND", "No approved questionnaire for this hospital")
    return ok({"id": q["id"], "name": q["name"], "questions": json.loads(q["questions_json"])})

def submit_questionnaire(conn, actor, appointment_id, answers):
    appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
    if not appt:
        return err("NOT_FOUND", "Appointment not found")
    denial = _authorize_appointment_access(conn, actor, dict(appt))
    if denial:
        return denial
    q = conn.execute("SELECT * FROM questionnaires WHERE hospital_id=? AND status='approved' LIMIT 1", (appt["hospital_id"],)).fetchone()
    if not q:
        return err("NOT_FOUND", "No questionnaire configured")
    conn.execute("INSERT INTO questionnaire_responses(questionnaire_id,appointment_id,answers_json) VALUES (?,?,?)",
                 (q["id"], appointment_id, json.dumps(answers)))
    doctor_user = conn.execute("SELECT user_id FROM doctors WHERE id=?", (appt["doctor_id"],)).fetchone()
    if doctor_user:
        wf.send_notification(conn, doctor_user["user_id"], appointment_id, "questionnaire_completed",
                              "A patient completed their pre-visit questionnaire.")
    audit(conn, actor["email"], actor["role"], appt["hospital_id"], "submit_questionnaire", "appointment", appointment_id)
    conn.commit()
    return ok({"submitted": True})

# ---------- notifications / workflow / escalation ----------

def send_notification(conn, actor, user_id, appointment_id, ntype, message):
    wf.send_notification(conn, user_id, appointment_id, ntype, message)
    audit(conn, actor["email"], actor["role"], None, "send_notification", "user", user_id, {"type": ntype})
    conn.commit()
    return ok({"sent": True})

def start_workflow(conn, actor, appointment_id, wf_type):
    wf_id = wf.start_workflow(conn, appointment_id, wf_type)
    audit(conn, actor["email"], actor["role"], None, "start_workflow", "workflow", wf_id, {"type": wf_type})
    conn.commit()
    return ok({"workflow_id": wf_id})

def transfer_to_human(conn, actor, appointment_id, reason):
    appt = conn.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
    conn.execute("INSERT INTO reconciliation_records(appointment_id, status, details) VALUES (?, 'escalated', ?)",
                 (appointment_id, reason))
    audit(conn, actor["email"], actor["role"], appt["hospital_id"] if appt else None, "transfer_to_human",
          "appointment", appointment_id, {"reason": reason})
    conn.commit()
    return ok({"escalated": True, "reason": reason})
