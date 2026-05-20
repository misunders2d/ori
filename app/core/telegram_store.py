"""Async SQLite store for Telegram skill state.

Backs three independent concerns layered into one DB file
(``data/telegram_skills.db``) per proposal §2.2 / §2.3:

1. ``chat_aliases`` — owner-scoped alias → ``chat_id`` map for groups,
   supergroups, and channels. DM aliases are intentionally NOT permitted
   in v1; the cross-platform roster (``app/core/roster.py``) is the
   correct namespace for DM targets.
2. ``outbound_files`` — owner-scoped TTL cache of Telegram ``file_id``
   values so the bot can re-forward photos / documents / audio / video /
   voice / video_note across chats without re-uploading bytes.
3. ``last_forward_cache`` (in-memory only) — 5-minute capture of the most
   recent forwarded message per DM session_id; backs the no-arg
   ``/alias save <name>`` shorthand.

I/O contract (Law 5):

- All disk operations use ``aiosqlite``.
- Schema is created lazily on first call via ``_ensure_schema`` (no
  import-time DB writes).
- Each operation opens its own short-lived connection via the
  ``async with aiosqlite.connect(...)`` context manager.

TTL contract:

- File cache default: ``TELEGRAM_FILE_CACHE_TTL_HOURS`` env, default 168
  (7 days). ``get_file`` slides ``expires_at`` forward by the TTL on a
  successful hit. Expired rows are pruned on lookup.
- Last-forward cache: 5 minutes, in-memory only, lost on restart by
  design.

Failure mode (Law 6):

- Validation failures (unknown ``chat_type``, unknown ``file_type``)
  raise ``ValueError`` — callers must surface verbatim to the agent or
  return ``{"status": "error", ...}``.
- aiosqlite errors propagate; tool-layer callers wrap them per Rule 13.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = os.path.abspath("./data/telegram_skills.db")

ALLOWED_ALIAS_CHAT_TYPES = frozenset({"channel", "supergroup", "group"})
ALLOWED_FILE_TYPES = frozenset(
    {"photo", "document", "audio", "video", "voice", "video_note"}
)

DEFAULT_FILE_TTL_HOURS = 168  # 7 days
LAST_FORWARD_TTL_SECONDS = 5 * 60

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_aliases (
    owner_user_id TEXT NOT NULL,
    alias         TEXT NOT NULL,
    chat_id       INTEGER NOT NULL,
    chat_type     TEXT NOT NULL,
    title         TEXT,
    username      TEXT,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    PRIMARY KEY (owner_user_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_chat_aliases_owner ON chat_aliases(owner_user_id);

CREATE TABLE IF NOT EXISTS outbound_files (
    file_ref          TEXT NOT NULL,
    owner_user_id     TEXT NOT NULL,
    file_id           TEXT NOT NULL,
    file_type         TEXT NOT NULL,
    filename          TEXT,
    mime_type         TEXT,
    source_chat_id    INTEGER,
    source_message_id INTEGER,
    caption           TEXT,
    created_at        TEXT NOT NULL,
    expires_at        TEXT NOT NULL,
    PRIMARY KEY (owner_user_id, file_ref)
);
CREATE INDEX IF NOT EXISTS idx_outbound_files_expires ON outbound_files(expires_at);
"""


_schema_init_lock = asyncio.Lock()
_schema_initialized = False


# --------------------------------------------------------------------------- helpers


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """ISO-8601 UTC with microseconds; sortable lexicographically."""
    return dt.isoformat(timespec="microseconds")


def _now_iso() -> str:
    return _iso(_now_dt())


def _file_ttl() -> timedelta:
    raw = os.environ.get("TELEGRAM_FILE_CACHE_TTL_HOURS", "")
    try:
        hours = int(raw) if raw else DEFAULT_FILE_TTL_HOURS
    except ValueError:
        logger.warning(
            "TELEGRAM_FILE_CACHE_TTL_HOURS=%r is not an int; falling back to default",
            raw,
        )
        hours = DEFAULT_FILE_TTL_HOURS
    return timedelta(hours=hours)


def _foldcase(value: str) -> str:
    return (value or "").strip().casefold()


def _validate_alias(value: str) -> str:
    folded = _foldcase(value)
    if not folded:
        raise ValueError("alias must be a non-empty string")
    return folded


def _validate_file_ref(value: str) -> str:
    folded = _foldcase(value)
    if not folded:
        raise ValueError("file_ref must be a non-empty string")
    return folded


def _validate_chat_type(value: str) -> str:
    norm = (value or "").strip().lower()
    if norm not in ALLOWED_ALIAS_CHAT_TYPES:
        raise ValueError(
            f"chat_type must be one of {sorted(ALLOWED_ALIAS_CHAT_TYPES)} "
            f"(v1 does not support DM aliases); got {value!r}"
        )
    return norm


def _validate_file_type(value: str) -> str:
    norm = (value or "").strip().lower()
    if norm not in ALLOWED_FILE_TYPES:
        raise ValueError(
            f"file_type must be one of {sorted(ALLOWED_FILE_TYPES)}; got {value!r}"
        )
    return norm


async def _ensure_schema() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    async with _schema_init_lock:
        if _schema_initialized:
            return
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.executescript(_SCHEMA_SQL)
            await conn.commit()
        _schema_initialized = True


def _reset_for_tests() -> None:
    """Drop the schema-init flag. Test-only helper."""
    global _schema_initialized
    _schema_initialized = False
    _last_forward_cache.clear()


# --------------------------------------------------------------------------- aliases


async def save_alias(
    owner_user_id: str,
    alias: str,
    chat_id: int,
    chat_type: str,
    title: str | None = None,
    username: str | None = None,
) -> None:
    """Upsert an alias for ``owner_user_id``.

    Re-saving the same alias replaces the row (refreshes title / username
    / chat_id / chat_type, resets created_at).
    """
    if not owner_user_id:
        raise ValueError("owner_user_id required")
    alias = _validate_alias(alias)
    chat_type = _validate_chat_type(chat_type)
    if not isinstance(chat_id, int):
        raise ValueError("chat_id must be int")
    await _ensure_schema()
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO chat_aliases (
                owner_user_id, alias, chat_id, chat_type,
                title, username, created_at, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(owner_user_id, alias) DO UPDATE SET
                chat_id    = excluded.chat_id,
                chat_type  = excluded.chat_type,
                title      = excluded.title,
                username   = excluded.username,
                created_at = excluded.created_at
            """,
            (owner_user_id, alias, chat_id, chat_type, title, username, now),
        )
        await conn.commit()


async def list_aliases(owner_user_id: str) -> list[dict[str, Any]]:
    if not owner_user_id:
        return []
    await _ensure_schema()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT alias, chat_id, chat_type, title, username,
                   created_at, last_used_at
              FROM chat_aliases
             WHERE owner_user_id = ?
             ORDER BY alias
            """,
            (owner_user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def resolve_alias(
    owner_user_id: str, alias: str
) -> dict[str, Any] | None:
    """Lookup + slide ``last_used_at``. Returns None on miss."""
    if not owner_user_id:
        return None
    alias = _validate_alias(alias)
    await _ensure_schema()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT alias, chat_id, chat_type, title, username,
                   created_at, last_used_at
              FROM chat_aliases
             WHERE owner_user_id = ? AND alias = ?
            """,
            (owner_user_id, alias),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        now = _now_iso()
        await conn.execute(
            """
            UPDATE chat_aliases SET last_used_at = ?
             WHERE owner_user_id = ? AND alias = ?
            """,
            (now, owner_user_id, alias),
        )
        await conn.commit()
    result = dict(row)
    result["last_used_at"] = now
    return result


async def delete_alias(owner_user_id: str, alias: str) -> bool:
    if not owner_user_id:
        return False
    alias = _validate_alias(alias)
    await _ensure_schema()
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "DELETE FROM chat_aliases WHERE owner_user_id = ? AND alias = ?",
            (owner_user_id, alias),
        )
        await conn.commit()
        return cursor.rowcount > 0


# --------------------------------------------------------------------------- outbound files


async def put_file(
    owner_user_id: str,
    file_ref: str,
    file_id: str,
    file_type: str,
    *,
    filename: str | None = None,
    mime_type: str | None = None,
    source_chat_id: int | None = None,
    source_message_id: int | None = None,
    caption: str | None = None,
    ttl: timedelta | None = None,
) -> dict[str, Any]:
    """Upsert an outbound-file row.

    Duplicate ``(owner_user_id, file_ref)`` overwrites and refreshes
    ``expires_at`` per the design doc (proposal §7.12 — duplicate
    `/savefile` semantics).
    """
    if not owner_user_id:
        raise ValueError("owner_user_id required")
    if not file_id:
        raise ValueError("file_id required")
    file_ref = _validate_file_ref(file_ref)
    file_type = _validate_file_type(file_type)
    await _ensure_schema()
    ttl = ttl or _file_ttl()
    now_dt = _now_dt()
    created_at = _iso(now_dt)
    expires_at = _iso(now_dt + ttl)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            """
            INSERT INTO outbound_files (
                file_ref, owner_user_id, file_id, file_type,
                filename, mime_type, source_chat_id, source_message_id,
                caption, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_user_id, file_ref) DO UPDATE SET
                file_id           = excluded.file_id,
                file_type         = excluded.file_type,
                filename          = excluded.filename,
                mime_type         = excluded.mime_type,
                source_chat_id    = excluded.source_chat_id,
                source_message_id = excluded.source_message_id,
                caption           = excluded.caption,
                created_at        = excluded.created_at,
                expires_at        = excluded.expires_at
            """,
            (
                file_ref,
                owner_user_id,
                file_id,
                file_type,
                filename,
                mime_type,
                source_chat_id,
                source_message_id,
                caption,
                created_at,
                expires_at,
            ),
        )
        await conn.commit()
    return {
        "file_ref": file_ref,
        "owner_user_id": owner_user_id,
        "file_id": file_id,
        "file_type": file_type,
        "created_at": created_at,
        "expires_at": expires_at,
    }


async def get_file(
    owner_user_id: str, file_ref: str, *, slide: bool = True
) -> dict[str, Any] | None:
    """Return a row if present AND not expired. Prunes expired entries.

    Slides ``expires_at`` forward by TTL on hit when ``slide=True``
    (proposal §2.6 step (g)).
    """
    if not owner_user_id:
        return None
    file_ref = _validate_file_ref(file_ref)
    await _ensure_schema()
    now_dt = _now_dt()
    now = _iso(now_dt)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        # Eager prune of THIS row if expired, so callers see a clean miss.
        await conn.execute(
            """
            DELETE FROM outbound_files
             WHERE owner_user_id = ? AND file_ref = ? AND expires_at < ?
            """,
            (owner_user_id, file_ref, now),
        )
        async with conn.execute(
            """
            SELECT file_ref, owner_user_id, file_id, file_type,
                   filename, mime_type, source_chat_id, source_message_id,
                   caption, created_at, expires_at
              FROM outbound_files
             WHERE owner_user_id = ? AND file_ref = ?
            """,
            (owner_user_id, file_ref),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            await conn.commit()
            return None
        result = dict(row)
        if slide:
            new_expires = _iso(now_dt + _file_ttl())
            await conn.execute(
                """
                UPDATE outbound_files SET expires_at = ?
                 WHERE owner_user_id = ? AND file_ref = ?
                """,
                (new_expires, owner_user_id, file_ref),
            )
            result["expires_at"] = new_expires
        await conn.commit()
    return result


async def list_files(owner_user_id: str) -> list[dict[str, Any]]:
    if not owner_user_id:
        return []
    await _ensure_schema()
    await prune_expired_files()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT file_ref, owner_user_id, file_id, file_type,
                   filename, mime_type, source_chat_id, source_message_id,
                   caption, created_at, expires_at
              FROM outbound_files
             WHERE owner_user_id = ?
             ORDER BY file_ref
            """,
            (owner_user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def delete_file(owner_user_id: str, file_ref: str) -> bool:
    if not owner_user_id:
        return False
    file_ref = _validate_file_ref(file_ref)
    await _ensure_schema()
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "DELETE FROM outbound_files WHERE owner_user_id = ? AND file_ref = ?",
            (owner_user_id, file_ref),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def prune_expired_files() -> int:
    """Delete all expired rows. Returns the deletion count."""
    await _ensure_schema()
    now = _now_iso()
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "DELETE FROM outbound_files WHERE expires_at < ?",
            (now,),
        )
        await conn.commit()
        return cursor.rowcount


# --------------------------------------------------------------------------- last-forward cache


@dataclass
class ForwardCapture:
    chat_id: int
    chat_type: str
    title: str | None = None
    username: str | None = None
    captured_at: datetime = field(default_factory=_now_dt)


_last_forward_cache: dict[str, ForwardCapture] = {}
_last_forward_lock = threading.Lock()


def stash_forward(
    session_id: str,
    chat_id: int,
    chat_type: str,
    title: str | None = None,
    username: str | None = None,
) -> None:
    if not session_id:
        return
    with _last_forward_lock:
        _last_forward_cache[session_id] = ForwardCapture(
            chat_id=chat_id,
            chat_type=chat_type,
            title=title,
            username=username,
        )


def peek_forward(session_id: str) -> ForwardCapture | None:
    """Return the captured forward if within TTL; prune stale entry."""
    if not session_id:
        return None
    with _last_forward_lock:
        entry = _last_forward_cache.get(session_id)
        if entry is None:
            return None
        age = _now_dt() - entry.captured_at
        if age.total_seconds() > LAST_FORWARD_TTL_SECONDS:
            _last_forward_cache.pop(session_id, None)
            return None
        return entry


def pop_forward(session_id: str) -> ForwardCapture | None:
    """Read and remove the captured forward if within TTL."""
    if not session_id:
        return None
    with _last_forward_lock:
        entry = _last_forward_cache.pop(session_id, None)
    if entry is None:
        return None
    age = _now_dt() - entry.captured_at
    if age.total_seconds() > LAST_FORWARD_TTL_SECONDS:
        return None
    return entry
