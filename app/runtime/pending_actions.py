import sqlite3
import json
import secrets
import string
import time
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.path.abspath("./data/pending_actions.db")

def _init_db():
    """Initializes the SQLite database for pending actions."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_actions (
                    token TEXT PRIMARY KEY,
                    tool_name TEXT NOT NULL,
                    args TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
    except Exception as e:
        logger.error(f"Failed to initialize pending_actions DB: {e}")

def stage_action(tool_name: str, args: dict, user_id: str, session_id: str, ttl_minutes: int = 15) -> str:
    """Stages a tool call and returns a unique approval token.
    
    Args:
        tool_name: The name of the tool to be executed.
        args: The arguments for the tool call.
        user_id: The ID of the user who initiated the call.
        session_id: The current session ID.
        ttl_minutes: Time-to-live in minutes.
        
    Returns:
        str: A unique token (e.g., 'ACT-8A4F9X').
    """
    _init_db()
    suffix = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    token = f"ACT-{suffix}"
    
    expires_at = time.time() + (ttl_minutes * 60)
    
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO pending_actions (token, tool_name, args, user_id, session_id, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (token, tool_name, json.dumps(args), user_id, session_id, expires_at)
            )
        logger.info(f"Staged action {token} for tool {tool_name} (user: {user_id})")
        return token
    except Exception as e:
        logger.error(f"Failed to stage action {token}: {e}")
        raise

def get_and_delete_action(token: str) -> dict | None:
    """Retrieves and immediately deletes a staged action."""
    _init_db()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute(
                "SELECT tool_name, args, user_id, session_id, expires_at FROM pending_actions WHERE token = ?",
                (token,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            
            tool_name, args_json, user_id, session_id, expires_at = row
            conn.execute("DELETE FROM pending_actions WHERE token = ?", (token,))
            
            if time.time() > expires_at:
                logger.warning(f"Action token {token} has expired.")
                return None
                
            return {
                "tool_name": tool_name,
                "args": json.loads(args_json),
                "user_id": user_id,
                "session_id": session_id
            }
    except Exception as e:
        logger.error(f"Error retrieving action token {token}: {e}")
        return None

def cleanup_expired():
    """Removes all expired tokens from the database."""
    _init_db()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM pending_actions WHERE expires_at < ?", (time.time(),))
    except Exception as e:
        logger.error(f"Failed to cleanup expired actions: {e}")
