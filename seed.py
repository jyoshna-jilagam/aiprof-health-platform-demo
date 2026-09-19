import json
from db import get_conn, init_db
from utils import hash_pw

def run():
    conn = get_conn()

    conn.execute("INSERT INTO users(email,password,role,name) VALUES (?,?,?,?)",
                 ("platform@aiprof.dev", hash_pw("password123"), "platform_admin", "Platform Admin"))

    h1 = conn.execute("INSERT INTO hospitals(name,address,status) VALUES (?,?, 'approved')",
                       ("Sunrise General Hospital", "12 MG Road, Bengaluru")).lastrowid
    h2 = conn.execute("INSERT INTO hospitals(name,address,status) VALUES (?,?, 'approved')",
                       ("Lakeview Medical Center", "45 Lake Ave, Hyderabad")).lastrowid

    ha1 = conn.execute("INSERT INTO users(email,password,role,hospital_id,name) VALUES (?,?,?,?,?)",
                        ("admin@sunrise.dev", hash_pw("password123"), "hospital_admin", h1, "Sunrise Admin")).lastrowid
    ha2 = conn.execute("INSERT INTO users(email,password,role,hospital_id,name) VALUES (?,?,?,?,?)",
                        ("admin@lakeview.dev", hash_pw("password123"), "hospital_admin", h2, "Lakeview Admin")).lastrowid

    doctors_seed = [
        (h1, "Ananya Rao", "Orthopedics", "Ortho", 30),
        (h1, "Vikram Shah", "General Medicine", "General", 20),
        (h1, "Priya Nair", "Cardiology", "Cardio", 30),
        (h2, "Karan Mehta", "Dermatology", "Derm", 20),
        (h2, "Sara Iqbal", "Pediatrics", "Peds", 20),
        (h2, "Ravi Kumar", "Ophthalmology", "Eye", 25),
    ]
    doctor_ids = {}
    for hospital_id, name, spec, dept, dur in doctors_seed:
        u = conn.execute("INSERT INTO users(email,password,role,hospital_id,name) VALUES (?,?,?,?,?)",
                          (f"{name.split()[0].lower()}@doctor.dev", hash_pw("password123"), "doctor", hospital_id, name)).lastrowid
        did = conn.execute(
            "INSERT INTO doctors(hospital_id,user_id,name,specialty,department,duration_minutes,status) VALUES (?,?,?,?,?,?, 'active')",
            (hospital_id, u, name, spec, dept, dur)).lastrowid
        doctor_ids[name] = did
        for weekday in range(0, 5):  # Mon-Fri
            conn.execute("INSERT INTO availability(doctor_id,weekday,start_time,end_time) VALUES (?,?,?,?)",
                         (did, weekday, "09:00", "13:00"))
            conn.execute("INSERT INTO availability(doctor_id,weekday,start_time,end_time) VALUES (?,?,?,?)",
                         (did, weekday, "14:00", "17:00"))

    patients_seed = [("Meera Joseph", "meera@patient.dev"), ("Arjun Verma", "arjun@patient.dev")]
    for name, email in patients_seed:
        u = conn.execute("INSERT INTO users(email,password,role,name) VALUES (?,?,?,?)",
                          (email, hash_pw("password123"), "patient", name)).lastrowid
        conn.execute("INSERT INTO patients(user_id,name,dob,phone) VALUES (?,?,?,?)",
                     (u, name, "1990-01-01", "9999999999"))

    admin_questions = [
        {"id": "reason", "type": "text", "label": "Reason for visit (administrative summary)"},
        {"id": "insurance", "type": "text", "label": "Insurance provider (if any)"},
        {"id": "first_visit", "type": "yes_no", "label": "Is this your first visit to this hospital?"},
        {"id": "preferred_contact", "type": "choice", "label": "Preferred contact method",
         "options": ["phone", "email", "sms"]},
    ]
    conn.execute("INSERT INTO questionnaires(hospital_id,name,questions_json,status) VALUES (?,?,?, 'approved')",
                 (h1, "Pre-Visit Administrative Questionnaire", json.dumps(admin_questions)))
    conn.execute("INSERT INTO questionnaires(hospital_id,name,questions_json,status) VALUES (?,?,?, 'approved')",
                 (h2, "Pre-Visit Administrative Questionnaire", json.dumps(admin_questions)))

    conn.commit()
    conn.close()
    print("Seed complete.")

if __name__ == "__main__":
    init_db(reset=True)
    run()
