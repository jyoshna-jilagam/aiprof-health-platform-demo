# Architecture

## Components
```
Browser (Flask templates + vanilla JS, Web Speech API)
        |
Flask routes (app.py) — session-based auth, role checks
        |
AI Agent (ai_agent.py) — intent extraction only (LLM optional / deterministic fallback)
        |
Capability layer (capabilities.py) — validation, authorization, tenant scoping, audit
        |
Scheduling service (scheduling.py) — source of truth for bookable slots, no LLM
        |
EHR connector interface (ehr.py) — MockEHR implementation, swappable
        |
SQLite (db.py / schema.sql) — durable state, partial unique index prevents double-booking
        |
Workflows (workflows.py) — notifications + reminder workflow, state kept separate from appointments
```
The AI never touches SQLite or the EHR directly — every action goes through `capabilities.py`.

## Data model (key tables)
`hospitals, users, doctors, availability, blocked_slots, patients, appointments,
external_mappings, ehr_operations, mock_ehr_appointments, questionnaires,
questionnaire_responses, workflows, notifications, audit_events, reconciliation_records`.

- Tenant ownership: `hospital_id` on doctors/appointments/questionnaires/audit rows.
- State separation: appointment status (transactional) vs. `workflows.status` (process)
  vs. `ehr_operations.status` (integration) vs. `reconciliation_records` (operational) are
  distinct tables, never merged into one blob.
- Double-booking prevention: `CREATE UNIQUE INDEX uq_active_slot ON appointments(doctor_id,
  slot_date, start_time) WHERE status IN (active statuses)` — a partial unique index, enforced
  by SQLite itself, not just application logic.

## Booking sequence
```
1. Authorize (role=patient, patient_id matches actor)
2. Validate doctor active + hospital approved
3. scheduling.try_reserve_slot() -> INSERT ... status='pending' (unique index catches races)
4. Create ehr_operations row (status='started', operation_id)
5. Map/create patient & provider external IDs (idempotent, cached in external_mappings)
6. ehr_connector.create_appointment(idempotency_key=...)
7. On success: verify_external_appointment -> synchronize_state -> status='confirmed'
8. Assign questionnaire (if configured) + run post-booking workflow (notifications)
9. Return structured result to caller (AI agent or UI)
```

## EHR integration flow
`AI -> capability -> scheduling -> ehr.EHRConnector (interface) -> MockEHR (implementation)
-> mock_ehr_appointments table (simulated external system)`. Vendor-specific logic lives only
inside `MockEHR`; a real connector implements the same 6-method interface
(`lookup_or_create_patient/provider`, `create/get/cancel/reschedule_appointment`).

## Failure / recovery flow (required demo)
```
create_appointment(simulate_ehr_timeout=True)
  -> MockEHR persists the external record FIRST (durable, as in a real system)
  -> then raises EHRTimeoutError to the caller (network/timeout simulation)
  -> ehr_operations.status = 'unknown'; appointment.status = 'sync_pending'
  -> booking service queries EHR by the STABLE idempotency_key (never blindly retries create)
     -> found  -> verify -> synchronize -> appointment.status = 'confirmed'
     -> not found -> appointment.status = 'reconciliation_required'
                     -> reconciliation_records row + transfer_to_human() escalation
```
This is exercised by `tests/test_core.py::test_ehr_timeout_recovers_without_duplicate` and
interactively via the "Failure Demo" page / the chat's "Simulate EHR timeout" checkbox.

## Tenant / security model
- Session holds `role`, `hospital_id`, `patient_id`/`doctor_id`; every capability function
  re-checks these against the target row (`_authorize_appointment_access`,
  `lookup_patient`), not just the UI route decorator.
- Patients: only their own `patient_id`. Doctors: only appointments where
  `doctor_id == actor.doctor_id`. Hospital admins: only rows with their `hospital_id`.
  Platform admin: cross-tenant, read/approve only, no direct booking capability.
- All capability calls write an `audit_events` row (actor, role, hospital, action, entity).
