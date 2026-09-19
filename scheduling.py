"""Scheduling service: deterministic slot calculation & conflict prevention.
No LLM involved. This is the single source of truth for bookable slots."""
import datetime, sqlite3

ACTIVE_STATUSES = ('requested', 'pending', 'confirmed', 'rescheduled', 'sync_pending', 'reconciliation_required')

def _doctor_active_and_hospital_approved(conn, doctor_id):
    row = conn.execute(
        """SELECT d.status as dstatus, h.status as hstatus, d.duration_minutes, d.hospital_id
           FROM doctors d JOIN hospitals h ON h.id = d.hospital_id WHERE d.id=?""", (doctor_id,)).fetchone()
    if not row:
        return None
    if row["dstatus"] != "active" or row["hstatus"] != "approved":
        return None
    return row

def get_available_slots(conn, doctor_id, days_ahead=7, appointment_type=None):
    """Generate real bookable slots for a doctor over the next `days_ahead` days."""
    row = _doctor_active_and_hospital_approved(conn, doctor_id)
    if not row:
        return []
    duration = row["duration_minutes"]
    today = datetime.date.today()
    avail_rows = conn.execute("SELECT * FROM availability WHERE doctor_id=?", (doctor_id,)).fetchall()
    avail_by_weekday = {}
    for a in avail_rows:
        avail_by_weekday.setdefault(a["weekday"], []).append(a)

    booked = set()
    for b in conn.execute(
        f"SELECT slot_date, start_time FROM appointments WHERE doctor_id=? AND status IN ({','.join('?'*len(ACTIVE_STATUSES))})",
        (doctor_id, *ACTIVE_STATUSES)).fetchall():
        booked.add((b["slot_date"], b["start_time"]))

    blocked = conn.execute("SELECT date, start_time, end_time FROM blocked_slots WHERE doctor_id=?", (doctor_id,)).fetchall()

    slots = []
    for d in range(days_ahead):
        date = today + datetime.timedelta(days=d)
        weekday = date.weekday()
        for a in avail_by_weekday.get(weekday, []):
            cur = datetime.datetime.combine(date, datetime.time.fromisoformat(a["start_time"]))
            end = datetime.datetime.combine(date, datetime.time.fromisoformat(a["end_time"]))
            while cur + datetime.timedelta(minutes=duration) <= end:
                start_s = cur.time().isoformat(timespec="minutes")
                end_t = (cur + datetime.timedelta(minutes=duration)).time().isoformat(timespec="minutes")
                date_s = date.isoformat()
                is_blocked = any(bl["date"] == date_s and start_s < bl["end_time"] and end_t > bl["start_time"] for bl in blocked)
                is_booked = (date_s, start_s) in booked
                is_past = datetime.datetime.combine(date, cur.time()) < datetime.datetime.now()
                if not is_blocked and not is_booked and not is_past:
                    slots.append({"date": date_s, "start_time": start_s, "end_time": end_t})
                cur += datetime.timedelta(minutes=duration)
    return slots

def is_slot_bookable(conn, doctor_id, date_s, start_time):
    """Revalidate a specific slot immediately before booking. Returns (bool, reason)."""
    row = _doctor_active_and_hospital_approved(conn, doctor_id)
    if not row:
        return False, "Doctor inactive or hospital not approved"
    duration = row["duration_minutes"]
    weekday = datetime.date.fromisoformat(date_s).weekday()
    avail = conn.execute("SELECT * FROM availability WHERE doctor_id=? AND weekday=?", (doctor_id, weekday)).fetchall()
    start_t = datetime.time.fromisoformat(start_time)
    end_dt = (datetime.datetime.combine(datetime.date.today(), start_t) + datetime.timedelta(minutes=duration)).time()
    end_time = end_dt.isoformat(timespec="minutes")
    within_hours = any(a["start_time"] <= start_time and end_time <= a["end_time"] for a in avail)
    if not within_hours:
        return False, "Outside working hours"
    blocked = conn.execute("SELECT * FROM blocked_slots WHERE doctor_id=? AND date=?", (doctor_id, date_s)).fetchall()
    if any(start_time < bl["end_time"] and end_time > bl["start_time"] for bl in blocked):
        return False, "Slot blocked"
    existing = conn.execute(
        f"SELECT id FROM appointments WHERE doctor_id=? AND slot_date=? AND start_time=? AND status IN ({','.join('?'*len(ACTIVE_STATUSES))})",
        (doctor_id, date_s, start_time, *ACTIVE_STATUSES)).fetchone()
    if existing:
        return False, "Slot already booked"
    if datetime.datetime.combine(datetime.date.fromisoformat(date_s), start_t) < datetime.datetime.now():
        return False, "Slot in the past"
    return True, end_time

def try_reserve_slot(conn, doctor_id, hospital_id, patient_id, date_s, start_time, idempotency_key):
    """Atomically create a 'pending' appointment. Relies on unique partial index for conflict prevention."""
    existing = conn.execute("SELECT * FROM appointments WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if existing:
        return dict(existing), None
    bookable, info = is_slot_bookable(conn, doctor_id, date_s, start_time)
    if not bookable:
        return None, info
    end_time = info
    try:
        cur = conn.execute(
            """INSERT INTO appointments(hospital_id,doctor_id,patient_id,slot_date,start_time,end_time,status,idempotency_key)
               VALUES (?,?,?,?,?,?, 'pending', ?)""",
            (hospital_id, doctor_id, patient_id, date_s, start_time, end_time, idempotency_key))
        conn.commit()
        appt = conn.execute("SELECT * FROM appointments WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(appt), None
    except sqlite3.IntegrityError:
        conn.rollback()
        return None, "Slot already booked (conflict)"
