import os
import sqlite3
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "data.db")
# Default retention: 365 days
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "365"))
# Vacuum can take time; default off unless explicitly enabled
DO_VACUUM = os.getenv("PRUNE_VACUUM", "0") == "1"

CUTOFF_ISO = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()

def main():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"DB not found at {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()

        # If your table name differs, adjust it here.
        # Common table name used in these bots: messages
        # And common timestamp column: timestamp or sent_at
        # We'll try a safe approach: detect columns.
        cur.execute("PRAGMA table_info(messages);")
        cols = [r[1] for r in cur.fetchall()]
        if not cols:
            raise RuntimeError("Table 'messages' not found. Adjust prune_db.py to match your schema.")

        # Choose timestamp column name used by your schema
        ts_col = None
        for candidate in ("sent_at_utc", "sent_at", "timestamp", "sent_at_epoch", "date"):
            if candidate in cols:
                ts_col = candidate
                break
        if ts_col is None:
            raise RuntimeError(f"Could not find a timestamp column in messages table. Found: {cols}")

        # Delete old rows
        cur.execute(f"SELECT COUNT(*) FROM messages WHERE {ts_col} < ?;", (CUTOFF_ISO,))
        to_delete = cur.fetchone()[0]

        cur.execute(f"DELETE FROM messages WHERE {ts_col} < ?;", (CUTOFF_ISO,))
        conn.commit()

        print(f"Pruned {to_delete} rows older than {RETENTION_DAYS} days (cutoff={CUTOFF_ISO}).")

        if DO_VACUUM:
            print("Running VACUUM (can take a while)...")
            cur.execute("VACUUM;")
            conn.commit()
            print("VACUUM complete.")

    finally:
        conn.close()

if __name__ == "__main__":
    main()
