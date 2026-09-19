"""Workflow & notification engine. Workflow state is tracked separately from
appointment (transactional) state, as required."""
import json
from utils import now

def start_workflow(conn, appointment_id, wf_type):
    history = [{"ts": now(), "event": "started"}]
    cur = conn.execute(
        "INSERT INTO workflows(appointment_id,type,status,history_json) VALUES (?,?,?,?)",
        (appointment_id, wf_type, "running", json.dumps(history)))
    conn.commit()
    return cur.lastrowid

def advance_workflow(conn, workflow_id, event, status=None):
    row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    if not row:
        return None
    history = json.loads(row["history_json"])
    history.append({"ts": now(), "event": event})
    new_status = status or row["status"]
    conn.execute("UPDATE workflows SET history_json=?, status=?, updated_at=? WHERE id=?",
                 (json.dumps(history), new_status, now(), workflow_id))
    conn.commit()
    return new_status

def send_notification(conn, user_id, appointment_id, ntype, message):
    conn.execute(
        "INSERT INTO notifications(user_id,appointment_id,type,message,status) VALUES (?,?,?,?, 'sent')",
        (user_id, appointment_id, ntype, message))
    conn.commit()

def run_post_booking_workflow(conn, appointment, patient_user_id, doctor_user_id):
    """Simulated reminder/notification workflow triggered right after a verified booking."""
    wf_id = start_workflow(conn, appointment["id"], "post_booking_reminder")
    send_notification(conn, patient_user_id, appointment["id"], "confirmation",
                       f"Your appointment on {appointment['slot_date']} at {appointment['start_time']} is confirmed.")
    advance_workflow(conn, wf_id, "confirmation_notification_sent")
    if doctor_user_id:
        send_notification(conn, doctor_user_id, appointment["id"], "new_appointment",
                           f"New appointment booked on {appointment['slot_date']} at {appointment['start_time']}.")
        advance_workflow(conn, wf_id, "doctor_notified")
    send_notification(conn, patient_user_id, appointment["id"], "reminder_scheduled",
                       "A reminder notification has been scheduled before your visit.")
    advance_workflow(conn, wf_id, "reminder_scheduled", status="completed")
    return wf_id
