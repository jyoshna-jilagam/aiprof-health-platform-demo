# AI.Prof Health — Multi-Hospital Intake, Scheduling & Pre-Visit Voice Agent

A working prototype of a multi-tenant healthcare access platform: patients describe their
need in natural language, an AI agent discovers real availability across hospitals, books
through a controlled capability layer, integrates with a Mock EHR with **verified**,
**recoverable** booking, assigns a pre-visit questionnaire, and triggers a notification
workflow — all visible to doctors, hospital admins and platform admins.

## Features implemented
- Role-based access: Platform Admin, Hospital Admin, Doctor, Patient (2 seeded hospitals, 6 doctors, 2 patients)
- Hospital/doctor setup: working hours, blocked slots, lifecycle status (draft→approved, invited→active)
- AI Patient Access Agent (text + browser voice) using a controlled capability layer only
- Deterministic fallback intent engine (always available) + optional LLM adapter (Anthropic API) when `ANTHROPIC_API_KEY` is set
- Real scheduling engine: working hours, blocked slots, doctor/hospital status, no double-booking (SQLite partial unique index)
- Mock EHR connector: patient/provider/appointment ID mapping, create/verify/reschedule/cancel
- **Required failure demo**: simulated EHR timeout after external creation, automatic recovery via idempotency key query, no duplicate booking, reconciliation escalation when unresolved
- Pre-visit administrative questionnaire, assigned after verified booking, reviewed by doctor
- Simulated reminder/notification workflow with execution history, kept separate from appointment state
- Dashboards for all 4 roles with audit log, integration activity, and reconciliation queue
- 9 automated tests covering scheduling, tenancy, idempotency, and the failure/recovery path

## Tech stack
Python 3.11+, Flask, SQLite, python-dotenv, pytest, vanilla JS + Web Speech API (voice), no external services required.

## Setup
```bash
cd app
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # optional: add ANTHROPIC_API_KEY to enable LLM intent extraction
python3 seed.py           # creates aiprof.db and seed data (auto-runs on first app start too)
python3 app.py            # http://localhost:5000
```

## Environment variables
| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | no (has dev default) | Flask session signing |
| `ANTHROPIC_API_KEY` | no | If set, the AI agent uses Claude for intent extraction; otherwise it runs the deterministic fallback (clearly labeled "fallback" in the chat UI) |

## Demo credentials (password: `password123` for all)
| Role | Email |
|---|---|
| Platform Admin | platform@aiprof.dev |
| Hospital Admin (Sunrise) | admin@sunrise.dev |
| Hospital Admin (Lakeview) | admin@lakeview.dev |
| Doctor | ananya@doctor.dev, vikram@doctor.dev, priya@doctor.dev, karan@doctor.dev, sara@doctor.dev, ravi@doctor.dev |
| Patient | meera@patient.dev, arjun@patient.dev |

## Tests
```bash
pytest tests/ -v
```
9/9 passing: availability calculation, double-booking prevention, appointment state
transitions, capability authorization, tenant isolation, idempotent booking, EHR timeout
recovery without duplicate, verification/sync, questionnaire+workflow trigger.

## Demo walkthrough
1. Log in as **meera@patient.dev** → **AI Assistant** → type "I need an appointment for my
   shoulder pain this week" → pick a doctor → pick a slot → booking is created, sent to the
   Mock EHR, verified, and confirmed; a questionnaire and reminder workflow are triggered.
2. **Failure recovery**: on the AI Assistant page, tick "Simulate EHR timeout after create"
   before sending your slot choice (or use the dedicated **Failure Demo** page). The Mock EHR
   persists the record, then the client simulates a timeout. The booking service marks the
   outcome unknown, queries the EHR by idempotency key, finds the already-created record,
   verifies and synchronizes it, and confirms — without creating a duplicate. The full
   operation trace (creation → timeout → recovery → final state) is shown on screen.
3. Log in as a doctor to see appointments and questionnaire responses.
4. Log in as a hospital admin to configure doctors/availability and see integration/workflow activity.
5. Log in as the platform admin to approve hospitals, view the reconciliation queue and audit log.

## Deployment
Runs as a standard Flask app (`python3 app.py` for dev, or `gunicorn app:app` behind a
reverse proxy for production). SQLite file (`aiprof.db`) is created on first run — for a
real deployment, mount a persistent volume for it or point `db.DB_PATH` at a managed Postgres
instance instead (the code isolates all SQL in `db.py`/module functions, so swapping the
engine does not touch business logic).

## Known limitations
- Voice is browser-based (Web Speech API) with text fallback; telephony (PSTN) is out of
  scope per the requirements' "do not make telephony a blocker" guidance.
- The LLM adapter is a thin optional layer for intent extraction only; all business logic
  (availability, authorization, booking, verification) is deterministic and LLM-independent.
- Single-process SQLite is sufficient for this prototype's concurrency needs; a production
  deployment would move to Postgres with row-level tenant policies.
- Notifications are simulated (stored + shown in-app), not sent via real email/SMS providers.

## Future improvements
- Real EHR connector (e.g., FHIR-based) implementing the same `EHRConnector` interface
- Real SMS/email notification providers behind the existing `send_notification` capability
- Telephony channel reusing the same AI agent and capability layer
- AI evaluation harness scoring intent accuracy and safety-boundary adherence

## PRD completion updates (v2 prototype)

This version adds the highest-priority PRD gaps while keeping the existing capability-first architecture:

- Conversational appointment lookup with doctor/hospital details and appointment ID.
- Conversational rescheduling: retrieve current appointment, show real alternative slots, reschedule through the capability layer, verify/synchronize the Mock EHR, and notify the patient.
- Conversational pre-visit questionnaire collection with structured answers and doctor notification.
- Patient profile and communication preferences.
- Patient-owned questionnaire route protection.
- Hospital self-service registration with Submitted → platform approval flow.
- Hospital configuration for departments, specialties, appointment types, communication preferences, and integration name.
- Doctor-controlled availability and blocked-time management.
- Platform operational metrics for AI capability actions, completed workflows, and booking failures.
- Existing Mock EHR timeout/unknown-outcome recovery and audit trail remain intact.

### Verification

The automated suite contains **14 passing tests**, including availability, double-booking prevention, capability authorization, tenant isolation, idempotency, EHR timeout recovery, questionnaire/workflow triggering, cancellation, AI appointment lookup, AI conversational rescheduling, conversational questionnaire collection, and patient questionnaire ownership.

### Remaining environment-dependent scope

The PRD describes telephone/PSTN support in addition to web voice. The prototype provides browser voice through the Web Speech API; a real telephone carrier/telephony integration is intentionally not hard-coded because no telephony provider credentials/configuration are supplied by the PRD. The integration boundary can be connected to a provider later.

Notifications are represented in the application's notification/workflow tables for the prototype; external SMS/email delivery is not configured by default.
