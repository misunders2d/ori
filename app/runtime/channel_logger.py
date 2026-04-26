import logging
import os
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DB_PATH = os.path.abspath("./data/channel_logs.db")

def _init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS channel_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                user_id TEXT,
                display_name TEXT,
                text TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_id ON channel_logs(chat_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON channel_logs(timestamp)")
        conn.commit()
        conn.close()
    except sqlite3.OperationalError as e:
        logger.warning("channel_logs DB init failed: %s", e)

# Initialize on import
_init_db()

def log_message(chat_id: str, user_id: str, display_name: str, text: str):
    """Log a message from a channel/group."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO channel_logs (chat_id, user_id, display_name, text)
            VALUES (?, ?, ?, ?)
        """, (chat_id, user_id, display_name, text))
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to log message to SQLite for chat %s", chat_id)

def get_logs(chat_id: str, limit: int = 100, hours: int = 24) -> list[dict]:
    """Retrieve recent logs for a specific chat."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        since = datetime.utcnow() - timedelta(hours=hours)
        cursor.execute("""
            SELECT user_id, display_name, text, timestamp
            FROM channel_logs
            WHERE chat_id = ? AND timestamp >= ?
            ORDER BY timestamp DESC
            LIMIT ?
        """, (chat_id, since.strftime('%Y-%m-%d %H:%M:%S'), limit))

        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception:
        logger.exception("Failed to retrieve logs for chat %s", chat_id)
        return []

def purge_old_logs(days: int = 7):
    """Delete logs older than the specified number of days."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        since = datetime.utcnow() - timedelta(days=days)
        cursor.execute("DELETE FROM channel_logs WHERE timestamp < ?", (since.strftime('%Y-%m-%d %H:%M:%S'),))
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to purge old logs")
