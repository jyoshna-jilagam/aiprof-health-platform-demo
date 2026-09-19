import hashlib, uuid, json, datetime

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def new_id(prefix="op"): return f"{prefix}_{uuid.uuid4().hex[:12]}"

def now(): return datetime.datetime.utcnow().isoformat(timespec="seconds")

def audit(conn, actor, role, hospital_id, action, entity_type=None, entity_id=None, details=None):
    conn.execute(
        "INSERT INTO audit_events(actor,role,hospital_id,action,entity_type,entity_id,details) VALUES (?,?,?,?,?,?,?)",
        (actor, role, hospital_id, action, entity_type, str(entity_id) if entity_id is not None else None,
         json.dumps(details) if details is not None else None))

class CapError(Exception):
    """Raised by capability layer on validation/authorization failure."""
    def __init__(self, code, message):
        self.code = code; self.message = message
        super().__init__(message)

def ok(data=None, **extra):
    r = {"success": True, "data": data or {}}
    r.update(extra)
    return r

def err(code, message):
    return {"success": False, "error": {"code": code, "message": message}}
