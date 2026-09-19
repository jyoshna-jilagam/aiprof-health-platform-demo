"""Healthcare-system integration layer.
EHRConnector defines the vendor-neutral interface. MockEHR is the only implementation
here; a real connector (Epic/Cerner/etc.) would implement the same interface so the
booking service never changes. Vendor-specific logic stays inside the connector.
"""
from utils import new_id

class EHRTimeoutError(Exception):
    """Raised when the external system created the record but the client never
    received the response (simulated network/timeout failure)."""
    pass

class EHRConnector:
    def lookup_or_create_patient(self, conn, patient_id): raise NotImplementedError
    def lookup_or_create_provider(self, conn, doctor_id): raise NotImplementedError
    def create_appointment(self, conn, *, idempotency_key, patient_external_id, provider_external_id,
                            date, start_time, end_time, simulate_timeout=False): raise NotImplementedError
    def get_appointment(self, conn, external_id=None, idempotency_key=None): raise NotImplementedError
    def cancel_appointment(self, conn, external_id): raise NotImplementedError
    def reschedule_appointment(self, conn, external_id, date, start_time, end_time): raise NotImplementedError


class MockEHR(EHRConnector):
    """A persistent mock external healthcare system, stored in its own SQLite table
    to emulate a genuinely separate system boundary."""

    def lookup_or_create_patient(self, conn, patient_id):
        row = conn.execute("SELECT external_id FROM external_mappings WHERE entity_type='patient' AND internal_id=?",
                            (patient_id,)).fetchone()
        if row:
            return row["external_id"]
        ext_id = new_id("extpat")
        conn.execute("INSERT INTO external_mappings(entity_type,internal_id,external_id) VALUES ('patient',?,?)",
                     (patient_id, ext_id))
        conn.commit()
        return ext_id

    def lookup_or_create_provider(self, conn, doctor_id):
        row = conn.execute("SELECT external_id FROM external_mappings WHERE entity_type='provider' AND internal_id=?",
                            (doctor_id,)).fetchone()
        if row:
            return row["external_id"]
        ext_id = new_id("extprov")
        conn.execute("INSERT INTO external_mappings(entity_type,internal_id,external_id) VALUES ('provider',?,?)",
                     (doctor_id, ext_id))
        conn.commit()
        return ext_id

    def create_appointment(self, conn, *, idempotency_key, patient_external_id, provider_external_id,
                            date, start_time, end_time, simulate_timeout=False):
        """Creates the record in the 'external system' first (this always succeeds and
        is durable), then optionally simulates the response never reaching the caller.
        This models the real-world case where the create succeeded server-side but the
        network call to the client timed out."""
        existing = conn.execute("SELECT * FROM mock_ehr_appointments WHERE idempotency_key=?",
                                 (idempotency_key,)).fetchone()
        if existing:
            external_id = existing["external_id"]
        else:
            external_id = new_id("extappt")
            conn.execute(
                """INSERT INTO mock_ehr_appointments(external_id,idempotency_key,patient_external_id,
                   provider_external_id,date,start_time,end_time,status) VALUES (?,?,?,?,?,?,?, 'booked')""",
                (external_id, idempotency_key, patient_external_id, provider_external_id, date, start_time, end_time))
            conn.commit()
        if simulate_timeout:
            raise EHRTimeoutError(f"Simulated timeout after external creation (external_id={external_id})")
        return {"external_id": external_id, "status": "booked"}

    def get_appointment(self, conn, external_id=None, idempotency_key=None):
        if external_id:
            row = conn.execute("SELECT * FROM mock_ehr_appointments WHERE external_id=?", (external_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM mock_ehr_appointments WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return dict(row) if row else None

    def cancel_appointment(self, conn, external_id):
        conn.execute("UPDATE mock_ehr_appointments SET status='cancelled' WHERE external_id=?", (external_id,))
        conn.commit()
        return self.get_appointment(conn, external_id=external_id)

    def reschedule_appointment(self, conn, external_id, date, start_time, end_time):
        conn.execute("UPDATE mock_ehr_appointments SET date=?, start_time=?, end_time=? WHERE external_id=?",
                     (date, start_time, end_time, external_id))
        conn.commit()
        return self.get_appointment(conn, external_id=external_id)


ehr_connector = MockEHR()  # single active connector instance (swap for a real one later)
