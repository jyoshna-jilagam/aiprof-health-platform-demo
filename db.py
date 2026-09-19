import sqlite3, os

# On Vercel (and other read-only-filesystem serverless hosts), the project
# directory can't be written to -- only /tmp is writable, and it's wiped
# between cold starts (so data won't persist across invocations there).
# Locally, keep the DB next to the source so it persists across runs.
if os.environ.get("VERCEL"):
    DB_PATH = "/tmp/aiprof.db"
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), "aiprof.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db(reset=False):
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = get_conn()
    with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
