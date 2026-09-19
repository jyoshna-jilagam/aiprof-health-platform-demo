# Integration Documentation

## Connector interface (`ehr.py`)
```python
class EHRConnector:
    def lookup_or_create_patient(self, conn, patient_id): ...
    def lookup_or_create_provider(self, conn, doctor_id): ...
    def create_appointment(self, conn, *, idempotency_key, patient_external_id,
                            provider_external_id, date, start_time, end_time,
                            simulate_timeout=False): ...
    def get_appointment(self, conn, external_id=None, idempotency_key=None): ...
    def cancel_appointment(self, conn, external_id): ...
    def reschedule_appointment(self, conn, external_id, date, start_time, end_time): ...
```
A real EHR (Epic, Cerner, a FHIR server, etc.) is added by implementing this interface and
swapping the `ehr_connector` singleton in `ehr.py` — no other module changes.

## Mock EHR operations
`MockEHR` stores its own external state in `mock_ehr_appointments`, a table that stands in for
a genuinely separate system: it is keyed by its own `external_id` and a unique
`idempotency_key`, and it is never read or written by any other module except through the
connector interface.

## ID mapping
`external_mappings(entity_type, internal_id, external_id, hospital_id)` maps:
- `patient` → external patient ID (created on first booking, then cached/reused)
- `provider` → external provider ID (created on first booking, then cached/reused)

`appointments.external_appointment_id` stores the mapped external appointment ID once verified.

## Verification
`verify_external_appointment` (capability) / `_verify_and_sync` (internal) re-fetches the
record from the connector by `external_id` and checks `status == 'booked'` before the
appointment is marked `confirmed`. A booking is never reported as successful based solely on
the initial API response — verification against the external record is mandatory.

## Idempotency
Every booking request carries an `idempotency_key` (auto-generated if not supplied) stored on
both the internal `appointments` row and the external `mock_ehr_appointments` row. Re-issuing
`create_appointment` with the same key returns the same appointment rather than creating a
second one — proven by `test_idempotent_booking_returns_same_appointment`.

## Failure recovery (unknown outcome)
```
create_appointment(..., simulate_ehr_timeout=True)
  1. MockEHR.create_appointment() inserts the external row (this always succeeds — it models
     the external system's own database commit).
  2. MockEHR then raises EHRTimeoutError to simulate the response never reaching the caller.
  3. capabilities.create_appointment catches EHRTimeoutError:
       - ehr_operations.status = 'unknown'
       - appointments.status = 'sync_pending'
       - calls ehr_connector.get_appointment(idempotency_key=...) — a READ, never a retry of
         the create — to discover the true state.
       - FOUND  -> verify + synchronize -> appointments.status = 'confirmed';
                   ehr_operations.status = 'succeeded' (recovered)
       - NOT FOUND -> appointments.status = 'reconciliation_required';
                      a reconciliation_records row is created and transfer_to_human() logs an
                      escalation for a human operator (visible on the Platform Admin dashboard).
```
The system never blindly retries `create_appointment` after an unknown outcome — it only ever
queries by the stable idempotency key, which is what prevents duplicate external appointments.

## Rescheduling / cancellation with verification
Both `reschedule_appointment` and `cancel_appointment` capabilities call the connector's
`reschedule_appointment` / `cancel_appointment` methods (when an external record exists),
capture the returned record as `verification`, and only then update the internal appointment
row's status — following the same "act, verify, synchronize" pattern as creation.
