"""Per-user OAuth2 token storage backed by SQLite.

Tokens are stored in data/oauth_tokens.db, keyed by user email.
Each row holds: email, access_token, refresh_token, token_expiry, scopes.
"""

import json
import logging
import os
import sqlite3
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH = os.path.abspath("./data/oauth_tokens.db")


def _get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            email TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            expiry REAL NOT NULL,
            scopes TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def save_token(email: str, access_token: str, refresh_token: str, expires_in: int, scopes: list[str]):
    """Save or update OAuth2 tokens for a user."""
    expiry = time.time() + expires_in
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tokens (email, access_token, refresh_token, expiry, scopes) VALUES (?, ?, ?, ?, ?)",
            (email, access_token, refresh_token, expiry, json.dumps(scopes)),
        )
        conn.commit()
        logger.info("OAuth token saved for %s", email)
    finally:
        conn.close()


def get_token(email: str) -> Optional[dict]:
    """Retrieve stored tokens for a user. Returns None if not found."""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT access_token, refresh_token, expiry, scopes FROM tokens WHERE email = ?", (email,)).fetchone()
        if not row:
            return None
        return {
            "access_token": row[0],
            "refresh_token": row[1],
            "expiry": row[2],
            "scopes": json.loads(row[3]),
            "expired": time.time() > row[2],
        }
    finally:
        conn.close()


def delete_token(email: str):
    """Remove stored tokens for a user."""
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM tokens WHERE email = ?", (email,))
        conn.commit()
    finally:
        conn.close()


def list_connected_users() -> list[str]:
    """Return list of emails with stored tokens."""
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT email FROM tokens").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()
