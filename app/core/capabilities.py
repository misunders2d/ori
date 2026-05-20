"""Per-user capability store layered on top of the binary whitelist.

The whitelist (``app/core/whitelist.py``) gates inbound traffic with a single
boolean: is this principal allowed to talk to the bot at all? Some operations
that flow OUT of the bot (sending to a group, mutating chat aliases,
forwarding files) need finer control. Rather than widen the whitelist schema
(which other consumers expect as ``list[str]``), this module adds an
orthogonal axis: capabilities granted per user_id.

Canonical capabilities (v1):
    - ``send_to_groups``  - call ``telegram_send_to_chat`` with a non-DM target.
    - ``manage_aliases``  - create or delete chat aliases.
    - ``forward_files``   - call ``telegram_forward`` with a non-DM target.

Listing and resolving aliases are self-scope and need no capability.

Admin implicit grant: any user_id in ``ADMIN_USER_IDS`` (env) has every
capability. Matches the whitelist module's admin-bypass pattern at
``app/core/whitelist.py:26-33``.

Persistence: ``data/capabilities.json``:
    {"tg_111": ["send_to_groups", "manage_aliases"], ...}

Atomic write contract (proposal §2.1):
    1. Module-level ``asyncio.Lock`` serializes concurrent mutations.
    2. Write to ``<final>.tmp`` in the same directory (same FS -> atomic).
    3. ``f.flush()`` + ``os.fsync(fd)`` push bytes to disk.
    4. ``os.replace(tmp, final)`` - atomic on POSIX.
    5. Lock released in ``finally``.

Corrupt-load rule (proposal §2.1 + §6):
    - First load with corrupt file: return empty dict, ERROR log, leave file.
    - Subsequent reload with corrupt file: keep stale cache, ERROR log, leave file.
    - Mutation after corrupt-load atomically replaces the bad file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Iterable

logger = logging.getLogger(__name__)

CAPABILITIES_PATH = os.path.abspath("./data/capabilities.json")
CANONICAL_CAPABILITIES = frozenset(
    {"send_to_groups", "manage_aliases", "forward_files"}
)

_lock = asyncio.Lock()
_cache: dict[str, set[str]] | None = None  # None == not yet loaded


# --------------------------------------------------------------------------- helpers


def _admin_ids() -> set[str]:
    raw = os.environ.get("ADMIN_USER_IDS", "")
    return {u.strip() for u in raw.split(",") if u.strip()}


def _is_admin(user_id: str) -> bool:
    if not user_id:
        return False
    admins = _admin_ids()
    if user_id in admins:
        return True
    # Compat with whitelist.py: numeric IDs may be stored without the tg_ prefix.
    if user_id.startswith("tg_") and user_id[3:] in admins:
        return True
    if user_id.isdigit() and f"tg_{user_id}" in admins:
        return True
    return False


def _validate_capability(capability: str) -> None:
    if capability not in CANONICAL_CAPABILITIES:
        raise ValueError(
            f"Unknown capability '{capability}'. "
            f"Canonical set: {sorted(CANONICAL_CAPABILITIES)}."
        )


def _read_from_disk() -> dict[str, set[str]] | None:
    """Sync read. Returns parsed dict, or None on corrupt/unreadable file.

    Caller decides how to merge with in-memory state.
    """
    if not os.path.exists(CAPABILITIES_PATH):
        return {}
    try:
        with open(CAPABILITIES_PATH, "r") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            logger.error(
                "capabilities: %s top-level is not a dict; treating as corrupt",
                CAPABILITIES_PATH,
            )
            return None
        parsed: dict[str, set[str]] = {}
        for user_id, caps in raw.items():
            if not isinstance(user_id, str) or not isinstance(caps, list):
                logger.error(
                    "capabilities: malformed row for %r; treating as corrupt",
                    user_id,
                )
                return None
            cap_set: set[str] = set()
            for c in caps:
                if not isinstance(c, str):
                    logger.error(
                        "capabilities: non-string capability in row %r; treating as corrupt",
                        user_id,
                    )
                    return None
                cap_set.add(c)
            parsed[user_id] = cap_set
        return parsed
    except (OSError, json.JSONDecodeError) as e:
        logger.error("capabilities: failed to load %s: %s", CAPABILITIES_PATH, e)
        return None


def _write_to_disk_sync(data: dict[str, set[str]]) -> None:
    """Sync atomic write. Caller holds the asyncio lock.

    Steps: tempfile in same dir -> flush+fsync -> os.replace.
    """
    os.makedirs(os.path.dirname(CAPABILITIES_PATH), exist_ok=True)
    tmp_path = CAPABILITIES_PATH + ".tmp"
    serializable = {uid: sorted(caps) for uid, caps in sorted(data.items())}
    with open(tmp_path, "w") as f:
        json.dump(serializable, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, CAPABILITIES_PATH)


async def _ensure_loaded() -> dict[str, set[str]]:
    """Populate ``_cache`` from disk on first access.

    Applies the corrupt-load rule:
      - First load + corrupt -> cache becomes ``{}``, file untouched, ERROR.
      - Subsequent reload + corrupt -> cache preserved, file untouched, ERROR.
    """
    global _cache
    if _cache is not None:
        return _cache
    parsed = await asyncio.to_thread(_read_from_disk)
    if parsed is None:
        _cache = {}
    else:
        _cache = parsed
    return _cache


# --------------------------------------------------------------------------- public API


async def has_capability(user_id: str, capability: str) -> bool:
    """Return True if user holds the capability or is an admin.

    Unknown ``capability`` names raise ValueError so callers cannot pass
    typos that would silently deny.
    """
    _validate_capability(capability)
    if _is_admin(user_id):
        return True
    cache = await _ensure_loaded()
    return capability in cache.get(user_id, set())


def _deep_copy(cache: dict[str, set[str]]) -> dict[str, set[str]]:
    return {uid: set(caps) for uid, caps in cache.items()}


async def grant(user_id: str, capability: str) -> None:
    """Add ``capability`` to ``user_id``. Atomic in memory + on disk.

    The in-memory cache is only swapped to the new state AFTER
    ``_write_to_disk_sync`` returns successfully. If the disk write
    raises, the previous in-memory state is preserved — no half-applied
    grant lingers in memory waiting for the next mutation to persist it.
    """
    _validate_capability(capability)
    if not user_id:
        raise ValueError("user_id required")
    global _cache
    async with _lock:
        cache = await _ensure_loaded()
        new_cache = _deep_copy(cache)
        new_cache.setdefault(user_id, set()).add(capability)
        await asyncio.to_thread(_write_to_disk_sync, new_cache)
        _cache = new_cache
        logger.info("capabilities: granted %r to %r", capability, user_id)


async def revoke(user_id: str, capability: str) -> None:
    """Remove ``capability`` from ``user_id``. Atomic in memory + on disk.

    No-op if the user has no entry or does not hold the capability.
    Same swap-on-success contract as ``grant``.
    """
    _validate_capability(capability)
    if not user_id:
        raise ValueError("user_id required")
    global _cache
    async with _lock:
        cache = await _ensure_loaded()
        caps = cache.get(user_id)
        if not caps or capability not in caps:
            return
        new_cache = _deep_copy(cache)
        new_caps = new_cache.get(user_id, set())
        new_caps.discard(capability)
        if not new_caps:
            new_cache.pop(user_id, None)
        else:
            new_cache[user_id] = new_caps
        await asyncio.to_thread(_write_to_disk_sync, new_cache)
        _cache = new_cache
        logger.info("capabilities: revoked %r from %r", capability, user_id)


async def list_for(user_id: str) -> list[str]:
    """Return capabilities granted to ``user_id``.

    Admins get the full canonical set (implicit grant). Non-admins get
    only what's been explicitly granted.
    """
    if _is_admin(user_id):
        return sorted(CANONICAL_CAPABILITIES)
    cache = await _ensure_loaded()
    return sorted(cache.get(user_id, set()))


async def reload() -> None:
    """Re-read the capabilities file. Honors the corrupt-load rule:

    if the on-disk file is corrupt and a prior cache exists, the prior
    cache is preserved and the file is left untouched.
    """
    global _cache
    parsed = await asyncio.to_thread(_read_from_disk)
    if parsed is None:
        if _cache is None:
            _cache = {}
        # else: keep stale cache
        return
    _cache = parsed


async def all_users() -> list[str]:
    """Return the list of user_ids with at least one explicit grant.

    Does NOT include admins (their grants are implicit, not stored).
    """
    cache = await _ensure_loaded()
    return sorted(cache.keys())


def _reset_for_tests() -> None:
    """Drop in-memory cache. Test-only helper.

    Avoids relying on module reload semantics in pytest-asyncio fixtures.
    """
    global _cache
    _cache = None
