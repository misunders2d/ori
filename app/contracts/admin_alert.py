"""System-wide admin alert for contract failures.

The contract worker used to call ``run_emit("telegram_dm", {user_id, text})``
to surface failures, but the ``telegram_dm`` adapter routes through
``telegram_send_dm(person=...)`` which does a NAME lookup against the
roster. A numeric ``user_id`` like ``"330959414"`` never matches a
name, so the adapter silently returned ``{"status": "not_found"}``,
and the worker — which only watched for raised exceptions, not
``status`` values — counted it as delivered. Result: every contract
failure since the path was wired has FATALed without notifying anyone
(production proof: ai_pilot_wed_v3 on 2026-05-13 — 18:00 fire failed,
admin saw nothing until the chat-side panic).

This module replaces that path. Three independent layers make the
alert hard to silence:

  1. **Disk-first persistence.** Every alert is appended to
     ``data/contract_failures.jsonl`` BEFORE any transport is
     attempted. If every transport blows up, the failure is still
     readable via ``tail -f`` and any tool / cron job that wants to
     surface them later.
  2. **Direct Telegram send.** Skips the broken adapter chain.
     Resolves admin ``user_id``s through the roster to get the
     ``chat_id``, falls back to interpreting the id itself as a
     chat_id, and POSTs to ``api.telegram.org/bot<TOKEN>/sendMessage``
     directly. Status is only counted as ``delivered`` on
     ``HTTP 200 + ok=true``.
  3. **Loud diagnostic when nothing lands.** If 0 admins receive the
     alert (no token, no recipients, all sends failed), we write a
     second JSONL line marked ``alert_transport_failed`` with the
     reason, and emit ``logger.critical`` so journalctl / agent.log
     pick it up.

Public entry point: ``notify_admins(message, *, contract_id=None,
phase=None, audit_path=None)``. Async. Returns a delivery report.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FAILURE_LOG_PATH = os.path.abspath("./data/contract_failures.jsonl")


def _admin_user_ids() -> list[str]:
    """Read admin IDs from the same source the gate uses
    (``ADMIN_USER_IDS`` env, comma-separated). Empty list if unset —
    caller handles the no-recipients path."""
    raw = os.environ.get("ADMIN_USER_IDS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def _resolve_chat_id(user_id: str) -> Optional[int]:
    """Resolve an admin platform identifier (e.g. ``"330959414"``,
    ``"tg_330959414"``) to a Telegram chat_id.

    Strategy (first match wins):
      1. Roster entry by canonical user_id (e.g. ``"tg_330959414"``) —
         the bot's poller has been recording this every time the admin
         messages it.
      2. Roster entry by raw numeric (some legacy entries).
      3. The id ITSELF if it's a bare integer — Telegram chat_ids for
         private chats equal the user_id, so when the admin hasn't
         messaged the bot since the roster started recording, sending
         to ``int(user_id)`` still works.
    """
    from app.core.roster import get_entry

    candidates = [user_id]
    if user_id.startswith("tg_"):
        candidates.append(user_id[3:])  # raw numeric
    else:
        candidates.append(f"tg_{user_id}")  # prefixed

    for cand in candidates:
        entry = get_entry(cand)
        if entry and entry.get("chat_id"):
            try:
                return int(entry["chat_id"])
            except (TypeError, ValueError):
                continue

    # Fall back to interpreting the id itself as a chat_id (private
    # chats: chat_id == user_id).
    stripped = user_id[3:] if user_id.startswith("tg_") else user_id
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return None


async def _send_via_telegram_direct(chat_id: int, text: str) -> tuple[bool, str]:
    """Direct ``api.telegram.org/bot<TOKEN>/sendMessage`` call.

    Returns ``(delivered, detail)`` where ``delivered`` is True only on
    ``HTTP 200 + ok=true``. The ``detail`` is a short string for the
    failure log (the upstream error / non-200 status / no-token marker).
    """
    import httpx

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False, "TELEGRAM_BOT_TOKEN env empty"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # No Markdown — alerts are plain text. Eliminates the
    # "parse_mode rejected so the admin sees nothing" failure mode.
    payload = {"chat_id": chat_id, "text": text}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload)
    except Exception as e:
        return False, f"httpx error: {e!r}"

    if resp.status_code != 200:
        return False, f"http {resp.status_code}: {resp.text[:200]!r}"
    try:
        data = resp.json()
    except Exception:
        return False, f"non-json response: {resp.text[:200]!r}"
    if not data.get("ok"):
        return False, f"telegram ok=false: {data!r}"
    return True, "ok"


def _append_failure_log(record: dict[str, Any]) -> None:
    """Append one JSONL line to data/contract_failures.jsonl. Creates
    the parent directory on first use. Failures here are themselves
    logged at CRITICAL — the disk path is the last line of defence."""
    try:
        os.makedirs(os.path.dirname(_FAILURE_LOG_PATH), exist_ok=True)
        with open(_FAILURE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        logger.critical(
            "contract_failures.jsonl write failed for %r — alert disk path "
            "is broken, fix immediately",
            record.get("contract_id"),
        )


async def notify_admins(
    message: str,
    *,
    contract_id: Optional[str] = None,
    phase: Optional[str] = None,
    audit_path: Optional[str] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    """Surface a contract failure (or any administrative event) to
    every configured admin AND to the on-disk failure log.

    The on-disk log is written FIRST so transport failures can't hide
    the alert. Then we attempt direct Telegram sends to each admin.
    The return dict reports per-channel outcomes — callers can decide
    to escalate on ``delivered == 0``.

    Args:
      message: human-readable alert body (plain text — no Markdown).
      contract_id: contract id for log correlation.
      phase: worker phase string (``"emit_failed"``, ``"on_failure"``,
        etc.).
      audit_path: path to the per-fire audit JSONL on disk.
      error: original error message, copied into the on-disk record
        for grep-ability.

    Returns:
      ``{
        "delivered": int,
        "attempted": int,
        "admins": [{"user_id": str, "chat_id": int|None, "ok": bool, "detail": str}],
        "log_path": str,
      }``
    """
    ts = datetime.now(timezone.utc).isoformat()
    base_record = {
        "ts": ts,
        "contract_id": contract_id,
        "phase": phase,
        "audit_path": audit_path,
        "error": error,
        "message": message,
    }
    _append_failure_log(base_record)

    admins = _admin_user_ids()
    if not admins:
        # No recipients — disk log already captured the event.
        logger.critical(
            "CONTRACT FAILURE %s phase=%s: ADMIN_USER_IDS env is empty. "
            "Alert logged to %s; no Telegram delivery attempted.",
            contract_id,
            phase,
            _FAILURE_LOG_PATH,
        )
        _append_failure_log(
            {
                **base_record,
                "phase": "alert_transport_failed",
                "reason": "ADMIN_USER_IDS env empty",
            }
        )
        return {
            "delivered": 0,
            "attempted": 0,
            "admins": [],
            "log_path": _FAILURE_LOG_PATH,
        }

    report: list[dict[str, Any]] = []
    for user_id in admins:
        chat_id = _resolve_chat_id(user_id)
        if chat_id is None:
            report.append(
                {
                    "user_id": user_id,
                    "chat_id": None,
                    "ok": False,
                    "detail": "could not resolve to chat_id (roster miss + non-numeric id)",
                }
            )
            continue
        ok, detail = await _send_via_telegram_direct(chat_id, message)
        report.append(
            {"user_id": user_id, "chat_id": chat_id, "ok": ok, "detail": detail}
        )

    delivered = sum(1 for r in report if r["ok"])
    if delivered == 0:
        # Every admin failed. Persist a second line so the failure log
        # tells the full story without requiring journalctl access.
        logger.critical(
            "CONTRACT FAILURE %s phase=%s: 0/%d admins received the alert. "
            "Details: %s. See %s.",
            contract_id,
            phase,
            len(admins),
            report,
            _FAILURE_LOG_PATH,
        )
        _append_failure_log(
            {
                **base_record,
                "phase": "alert_transport_failed",
                "reason": "all-admin-transports-failed",
                "transport_report": report,
            }
        )

    return {
        "delivered": delivered,
        "attempted": len(admins),
        "admins": report,
        "log_path": _FAILURE_LOG_PATH,
    }


def boot_self_test_marker() -> str:
    """Return a path the scheduler boot path can ``touch`` after it
    successfully scheduled all contracts. Pure data — no side effects —
    so callers can decide their own policy. Currently used by the
    scheduler self-test to record the last successful boot time, so
    operators can spot a bot that crashed and restarted silently.
    """
    return os.path.abspath("./data/contract_scheduler_last_boot.txt")


def record_boot_ok() -> None:
    """Write the current timestamp to the boot marker file. Called
    after the scheduler finishes its boot-time admin-alert self-test.
    Cheap insurance — `stat` on the file tells operators when the
    contract subsystem last initialised cleanly.
    """
    path = boot_self_test_marker()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{time.time()}\n")
    except Exception:
        logger.warning("failed to write boot marker at %s", path)
