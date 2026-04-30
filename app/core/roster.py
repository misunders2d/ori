"""Cross-platform user roster — auto-populated as users message the bot.

Telegram bots cannot initiate DMs; they can only message users who have
previously contacted them. This module records every authorized inbound
sender so the agent can later look them up by name and DM them via the
registered transport adapter.

Stored at data/roster.json. Schema:

    {
      "<canonical_user_id>": {
        "platform": "telegram",
        "chat_id": 123456,
        "first_name": "...",
        "last_name": "...",
        "username": "...",
        "display_name": "...",
        "last_seen": "2026-04-30T12:34:56"
      },
      ...
    }
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

ROSTER_PATH = os.path.abspath("./data/roster.json")
_lock = threading.Lock()


def _load() -> dict[str, dict[str, Any]]:
    if not os.path.exists(ROSTER_PATH):
        return {}
    try:
        with open(ROSTER_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        logger.exception("roster: failed to load %s", ROSTER_PATH)
        return {}


def _save(data: dict[str, dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(ROSTER_PATH), exist_ok=True)
    tmp = ROSTER_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, ROSTER_PATH)


def record_user(
    user_id: str,
    platform: str,
    chat_id: str | int,
    first_name: str = "",
    last_name: str = "",
    username: str = "",
) -> None:
    """Upsert a roster entry. Called from transport pollers on each authorized inbound message."""
    display_name = (f"{first_name} {last_name}").strip() or username or str(chat_id)
    with _lock:
        data = _load()
        existing = data.get(user_id, {})
        existing.update(
            {
                "platform": platform,
                "chat_id": chat_id,
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
                "display_name": display_name,
                "last_seen": datetime.utcnow().isoformat(timespec="seconds"),
            }
        )
        data[user_id] = existing
        try:
            _save(data)
        except Exception:
            logger.exception("roster: failed to save")


def lookup_by_name(query: str, platform: str | None = None) -> list[dict[str, Any]]:
    """Case-insensitive substring search over display_name, first_name, last_name, username.

    Returns all matches; caller decides how to disambiguate. Filter by platform when given.
    """
    q = (query or "").strip().lower().lstrip("@")
    if not q:
        return []
    data = _load()
    matches: list[dict[str, Any]] = []
    for uid, entry in data.items():
        if platform and entry.get("platform") != platform:
            continue
        haystack = " ".join(
            str(entry.get(k, "") or "")
            for k in ("first_name", "last_name", "username", "display_name")
        ).lower()
        if q in haystack:
            matches.append({"user_id": uid, **entry})
    return matches


def get_entry(user_id: str) -> dict[str, Any] | None:
    return _load().get(user_id)
