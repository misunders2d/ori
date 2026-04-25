"""Periodic SQLite backup using the sqlite3 backup API."""

import logging
import os
import sqlite3
import time

logger = logging.getLogger(__name__)

BACKUP_DIR = os.path.abspath("./data/backups")
MAX_BACKUPS = 3


def backup_database(db_path: str, label: str):
    """Create a timestamped backup of a SQLite database.

    Uses sqlite3.backup() for an atomic, consistent copy that is safe
    even while the database is being read/written concurrently.
    Keeps the last MAX_BACKUPS backups per label and prunes older ones.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"{label}_{timestamp}.db")

    try:
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(backup_path)
        src.backup(dst)
        dst.close()
        src.close()
        logger.info("Backed up %s -> %s", db_path, backup_path)
    except Exception:
        logger.exception("Failed to backup %s", db_path)
        return

    # Prune old backups, keeping only the most recent MAX_BACKUPS
    existing = sorted(
        [f for f in os.listdir(BACKUP_DIR) if f.startswith(f"{label}_") and f.endswith(".db")],
        reverse=True,
    )
    for old in existing[MAX_BACKUPS:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
            logger.info("Pruned old backup: %s", old)
        except OSError:
            pass
