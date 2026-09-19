PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS hospitals(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, address TEXT,
  status TEXT NOT NULL DEFAULT 'submitted', -- draft/submitted/under_review/approved/rejected/suspended
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
  role TEXT NOT NULL, -- platform_admin/hospital_admin/doctor/patient
  hospital_id INTEGER REFERENCES hospitals(id),
  name TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS doctors(
  id INTEGER PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
  user_id INTEGER REFERENCES users(id), name TEXT NOT NULL, specialty TEXT, department TEXT,
  duration_minutes INTEGER DEFAULT 30, status TEXT DEFAULT 'active', -- invited/active/inactive/suspended
  external_provider_id TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_doctors_hospital ON doctors(hospital_id);

CREATE TABLE IF NOT EXISTS availability(
  id INTEGER PRIMARY KEY, doctor_id INTEGER NOT NULL REFERENCES doctors(id),
  weekday INTEGER NOT NULL, -- 0=Mon..6=Sun
  start_time TEXT NOT NULL, end_time TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_avail_doctor ON availability(doctor_id);

CREATE TABLE IF NOT EXISTS blocked_slots(
  id INTEGER PRIMARY KEY, doctor_id INTEGER NOT NULL REFERENCES doctors(id),
  date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL, reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_blocked_doctor ON blocked_slots(doctor_id, date);

CREATE TABLE IF NOT EXISTS patients(
  id INTEGER PRIMARY KEY, user_id INTEGER UNIQUE REFERENCES users(id),
  name TEXT NOT NULL, dob TEXT, phone TEXT, external_patient_id TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS appointments(
  id INTEGER PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
  doctor_id INTEGER NOT NULL REFERENCES doctors(id), patient_id INTEGER NOT NULL REFERENCES patients(id),
  slot_date TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'requested',
  -- requested/pending/confirmed/rescheduled/cancelled/completed/no_show/failed/sync_pending/reconciliation_required
  external_appointment_id TEXT, idempotency_key TEXT UNIQUE, operation_id TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_appt_hospital ON appointments(hospital_id);
CREATE INDEX IF NOT EXISTS idx_appt_patient ON appointments(patient_id);
CREATE INDEX IF NOT EXISTS idx_appt_doctor_date ON appointments(doctor_id, slot_date);
-- Prevents double booking: only one active appointment per doctor/date/start_time
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_slot ON appointments(doctor_id, slot_date, start_time)
  WHERE status IN ('requested','pending','confirmed','rescheduled','sync_pending','reconciliation_required');

CREATE TABLE IF NOT EXISTS external_mappings(
  id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, internal_id INTEGER NOT NULL,
  external_id TEXT NOT NULL, hospital_id INTEGER REFERENCES hospitals(id),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(entity_type, internal_id)
);

CREATE TABLE IF NOT EXISTS ehr_operations(
  id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, appointment_id INTEGER REFERENCES appointments(id),
  op_type TEXT NOT NULL, status TEXT NOT NULL, -- started/timeout/unknown/succeeded/failed
  request_json TEXT, response_json TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ehrops_appt ON ehr_operations(appointment_id);
CREATE INDEX IF NOT EXISTS idx_ehrops_opid ON ehr_operations(operation_id);

CREATE TABLE IF NOT EXISTS questionnaires(
  id INTEGER PRIMARY KEY, hospital_id INTEGER NOT NULL REFERENCES hospitals(id),
  name TEXT NOT NULL, questions_json TEXT NOT NULL, status TEXT DEFAULT 'approved'
);

CREATE TABLE IF NOT EXISTS questionnaire_responses(
  id INTEGER PRIMARY KEY, questionnaire_id INTEGER NOT NULL REFERENCES questionnaires(id),
  appointment_id INTEGER NOT NULL REFERENCES appointments(id), answers_json TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workflows(
  id INTEGER PRIMARY KEY, appointment_id INTEGER NOT NULL REFERENCES appointments(id),
  type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled', -- scheduled/running/completed/failed
  history_json TEXT DEFAULT '[]', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_workflow_appt ON workflows(appointment_id);

CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), appointment_id INTEGER REFERENCES appointments(id),
  type TEXT NOT NULL, message TEXT NOT NULL, status TEXT DEFAULT 'sent',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_events(
  id INTEGER PRIMARY KEY, actor TEXT, role TEXT, hospital_id INTEGER,
  action TEXT NOT NULL, entity_type TEXT, entity_id TEXT, details TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_hospital ON audit_events(hospital_id);

-- Simulates the external healthcare system's own database (separate from internal tables)
CREATE TABLE IF NOT EXISTS mock_ehr_appointments(
  external_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, patient_external_id TEXT,
  provider_external_id TEXT, date TEXT, start_time TEXT, end_time TEXT, status TEXT DEFAULT 'booked',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reconciliation_records(
  id INTEGER PRIMARY KEY, appointment_id INTEGER REFERENCES appointments(id), operation_id TEXT,
  status TEXT NOT NULL DEFAULT 'open', details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);


CREATE TABLE IF NOT EXISTS patient_preferences(
  id INTEGER PRIMARY KEY, patient_id INTEGER UNIQUE NOT NULL REFERENCES patients(id),
  preferred_time TEXT, communication_method TEXT, notes TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS doctor_profiles(
  id INTEGER PRIMARY KEY, doctor_id INTEGER UNIQUE NOT NULL REFERENCES doctors(id),
  photo_url TEXT, qualifications TEXT, experience_years INTEGER, languages TEXT, consultation_types TEXT
);

CREATE TABLE IF NOT EXISTS hospital_settings(
  id INTEGER PRIMARY KEY, hospital_id INTEGER UNIQUE NOT NULL REFERENCES hospitals(id),
  departments_json TEXT DEFAULT '[]', specialties_json TEXT DEFAULT '[]', appointment_types_json TEXT DEFAULT '[]',
  communication_preferences_json TEXT DEFAULT '{}', integration_name TEXT DEFAULT 'Mock EHR', updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
