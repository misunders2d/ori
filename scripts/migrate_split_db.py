"""One-time migration: split ori.db into ori-sessions.db + ori-scheduler.db."""

import os
import shutil
import sqlite3
import sys

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
OLD_DB = os.path.join(DATA_DIR, "ori.db")
SESSIONS_DB = os.path.join(DATA_DIR, "ori-sessions.db")
SCHEDULER_DB = os.path.join(DATA_DIR, "ori-scheduler.db")


def migrate():
    if not os.path.exists(OLD_DB):
        print("No ori.db found, nothing to migrate.")
        return

    if os.path.exists(SESSIONS_DB) or os.path.exists(SCHEDULER_DB):
        print("Target databases already exist — skipping migration.")
        return

    print(f"Splitting {OLD_DB} into session and scheduler databases...")

    # Copy entire old DB to both new files
    shutil.copy2(OLD_DB, SESSIONS_DB)
    shutil.copy2(OLD_DB, SCHEDULER_DB)

    # Drop APScheduler table from sessions DB
    conn = sqlite3.connect(SESSIONS_DB)
    conn.execute("DROP TABLE IF EXISTS apscheduler_jobs")
    conn.execute("VACUUM")
    conn.close()
    print(f"  Created {SESSIONS_DB} (APScheduler table removed)")

    # Drop ADK tables from scheduler DB
    conn = sqlite3.connect(SCHEDULER_DB)
    for table in ["sessions", "events", "app_states", "user_states", "adk_internal_metadata"]:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("VACUUM")
    conn.close()
    print(f"  Created {SCHEDULER_DB} (ADK tables removed)")

    # Rename old DB as backup
    backup_path = OLD_DB + ".bak"
    os.rename(OLD_DB, backup_path)
    print(f"  Renamed ori.db -> ori.db.bak")
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
