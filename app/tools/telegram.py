"""Telegram-specific agent tools.

`telegram_send_dm` was the original entry point (DM-by-name resolution
against the cross-platform roster). Slice 7 adds the rest of the
Telegram skill surface around aliases, send-to-chat, file forwarding,
and capability administration. All new tools follow the canonical
project conventions:

- ``tool_context: ToolContext = None`` on every signature.
- ``caller_user_id`` is read from ``tool_context.state["user_id"]``
  (populated by ``state_setter``); missing context returns an error
  rather than silently proceeding.
- ``send_to_groups`` / ``forward_files`` capability checks gate every
  non-DM outbound. Admin users hold every capability implicitly via
  ``app.core.capabilities`` (which mirrors the whitelist admin bypass).
- Telegram-side errors propagate ``description`` verbatim per Law 6.

Two write-side tools (``telegram_grant_capability`` /
``telegram_revoke_capability``) are listed here but are admin-gated by
``admin_tool_guardrail`` in slice 10 (ACT+TOTP staging).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from google.adk.tools.tool_context import ToolContext

from app.core import capabilities, telegram_store
from app.core.roster import lookup_by_name
from app.core.transport import get_adapter

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- helpers


def _caller_user_id(tool_context: ToolContext | None) -> str:
    """Best-effort extraction of the canonical caller id from the ADK
    tool_context. Returns '' when unavailable so callers can fail loud."""
    if tool_context is None:
        return ""
    state = getattr(tool_context, "state", None)
    if state is None:
        return ""
    try:
        value = state.get("user_id", "")
    except Exception:
        return ""
    return str(value or "")


def _is_admin(user_id: str) -> bool:
    """Mirror of the whitelist / capability admin check. Reads
    ``ADMIN_USER_IDS`` env at call time so test monkeypatches land."""
    if not user_id:
        return False
    raw = os.environ.get("ADMIN_USER_IDS", "")
    admin_ids = {u.strip() for u in raw.split(",") if u.strip()}
    if user_id in admin_ids:
        return True
    if user_id.startswith("tg_") and user_id[3:] in admin_ids:
        return True
    if user_id.isdigit() and f"tg_{user_id}" in admin_ids:
        return True
    return False


def _missing_caller_error() -> dict[str, Any]:
    return {
        "status": "error",
        "message": (
            "Missing caller user_id in tool_context.state — refusing to "
            "proceed (cannot enforce capability check)."
        ),
    }


async def _resolve_target(
    caller_user_id: str, target: str
) -> dict[str, Any]:
    """Resolve a target string to (chat_id, chat_type).

    Recognized forms:
      * ``tg_-1001234`` / ``tg_111`` — prefixed canonical id.
      * ``-1001234`` / ``111`` — raw signed/unsigned int.
      * any other token — alias lookup scoped to ``caller_user_id``.

    ``chat_type`` is ``None`` for raw / prefixed targets because we
    don't query Telegram for it; the cap-check helper falls back to
    "negative = non-DM" semantics in that case (proposal §12 v2-c).
    """
    t = str(target).strip() if target is not None else ""
    if not t:
        return {"ok": False, "message": "target is empty"}

    if t.startswith("tg_"):
        rest = t[3:]
        try:
            chat_id = int(rest)
        except ValueError:
            return {
                "ok": False,
                "message": f"invalid tg_-prefixed target: {target!r}",
            }
        return {"ok": True, "chat_id": chat_id, "chat_type": None, "alias": None}

    try:
        chat_id = int(t)
        return {"ok": True, "chat_id": chat_id, "chat_type": None, "alias": None}
    except ValueError:
        pass

    if not caller_user_id:
        return {"ok": False, "message": "alias lookup requires caller user_id"}
    try:
        row = await telegram_store.resolve_alias(caller_user_id, t)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        logger.exception(
            "telegram_store.resolve_alias failed for owner=%s alias=%s",
            caller_user_id,
            t,
        )
        return {"ok": False, "message": f"alias lookup failed: {e}"}
    if row is None:
        return {
            "ok": False,
            "message": (
                f"alias '{t}' not found for user {caller_user_id}. Save it "
                f"first via `/alias save <name>` or `telegram_save_alias`."
            ),
        }
    return {
        "ok": True,
        "chat_id": row["chat_id"],
        "chat_type": row["chat_type"],
        "alias": t,
    }


def _is_non_dm(chat_id: int, chat_type: str | None) -> bool:
    """Conservative classifier: an alias-resolved row carries chat_type
    (channel/supergroup/group → all non-DM). For raw chat_ids we assume
    a negative sign means non-DM (Telegram convention) and positive
    means DM-shaped (proposal §12 v2-c: fail loud rather than preflight)."""
    if chat_type in {"channel", "supergroup", "group"}:
        return True
    if chat_type in {"user", "private", "bot"}:
        return False
    return chat_id < 0


async def _require_capability(
    caller_user_id: str, capability: str
) -> dict[str, Any] | None:
    """Return an error dict if the caller lacks the capability; None on
    pass. Admins bypass via the implicit grant inside
    ``capabilities.has_capability``."""
    try:
        has = await capabilities.has_capability(caller_user_id, capability)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.exception(
            "capabilities.has_capability raised for user=%s cap=%s",
            caller_user_id,
            capability,
        )
        return {
            "status": "error",
            "message": f"capability check failed: {e}",
        }
    if has:
        return None
    return {
        "status": "error",
        "message": (
            f"User {caller_user_id} lacks capability '{capability}'. "
            f"An admin can grant via `/cap grant {caller_user_id} {capability}` "
            f"(deterministic) or `telegram_grant_capability(...)` (ACT+TOTP)."
        ),
    }


def _adapter_or_error():
    adapter = get_adapter("telegram")
    if adapter is None:
        return None, {
            "status": "error",
            "message": (
                "Telegram adapter is not registered (poller not running)."
            ),
        }
    return adapter, None


# --------------------------------------------------------------------------- send DM (existing — unchanged)


async def telegram_send_dm(
    person: str,
    text: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Send a direct message to a Telegram user by name or username.

    Resolves `person` against the local roster (auto-populated as users message
    the bot). If multiple roster entries match, returns the candidate list and
    asks the agent to disambiguate. The bot can only DM users who have messaged
    it at least once.

    Args:
        person: Display name, first/last name, or @username of the recipient.
        text: Message body.

    Returns:
        dict with `status`: "success" | "ambiguous" | "not_found" | "error".
    """
    matches = lookup_by_name(person, platform="telegram")
    if not matches:
        return {
            "status": "not_found",
            "message": (
                f"No telegram user matching '{person}' in the roster. "
                "Telegram bots can only DM users who have first messaged the bot. "
                "Ask the user to send any message to the bot, then retry."
            ),
        }
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "message": f"Multiple telegram users match '{person}'. Disambiguate by user_id.",
            "candidates": [
                {
                    "user_id": m["user_id"],
                    "display_name": m.get("display_name"),
                    "username": m.get("username"),
                }
                for m in matches
            ],
        }
    entry = matches[0]
    adapter, err = _adapter_or_error()
    if err is not None:
        return err
    try:
        await adapter.send_message(entry["chat_id"], text)
        return {
            "status": "success",
            "user_id": entry["user_id"],
            "display_name": entry.get("display_name"),
            "chat_id": entry["chat_id"],
        }
    except Exception as e:
        logger.exception("telegram_send_dm failed")
        return {"status": "error", "message": str(e)}


# --------------------------------------------------------------------------- send to chat (slice 7)


async def telegram_send_to_chat(
    target: str,
    text: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Send a text message to a group/channel/supergroup or by raw chat_id.

    `target` accepts: a saved alias (resolved against the caller's
    `chat_aliases`), a raw signed chat_id (e.g. ``-1001234567``), or a
    ``tg_-`` / ``tg_``-prefixed canonical id. Non-DM targets require the
    `send_to_groups` capability. The bot must already be a member of the
    target — no auto-join. Telegram-side errors propagate verbatim.
    """
    caller = _caller_user_id(tool_context)
    if not caller:
        return _missing_caller_error()

    resolved = await _resolve_target(caller, target)
    if not resolved.get("ok"):
        return {"status": "error", "message": resolved["message"]}
    chat_id = resolved["chat_id"]
    chat_type = resolved.get("chat_type")

    if _is_non_dm(chat_id, chat_type):
        err = await _require_capability(caller, "send_to_groups")
        if err is not None:
            return err

    adapter, err = _adapter_or_error()
    if err is not None:
        return err

    result = await adapter.send_text_strict(chat_id, text)
    if result.get("ok"):
        return {
            "status": "success",
            "chat_id": result.get("chat_id", chat_id),
            "message_id": result.get("message_id"),
            "alias": resolved.get("alias"),
        }
    return {
        "status": "error",
        "message": result.get("description", "Telegram returned no description."),
        "error_code": result.get("error_code", 0),
    }


# --------------------------------------------------------------------------- alias tools (slice 7)


async def telegram_save_alias(
    alias: str,
    chat_id: int | str,
    chat_type: str,
    title: str = "",
    username: str = "",
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Persist a chat alias for the caller. Requires `manage_aliases`.

    `chat_type` must be one of: `channel`, `supergroup`, `group`. DM
    aliases are intentionally not supported in v1 — the cross-platform
    roster owns the DM namespace.
    """
    caller = _caller_user_id(tool_context)
    if not caller:
        return _missing_caller_error()
    err = await _require_capability(caller, "manage_aliases")
    if err is not None:
        return err

    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "message": f"chat_id must be an integer; got {chat_id!r}",
        }
    try:
        await telegram_store.save_alias(
            owner_user_id=caller,
            alias=alias,
            chat_id=cid,
            chat_type=chat_type,
            title=title or None,
            username=username or None,
        )
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.exception(
            "telegram_store.save_alias failed for owner=%s alias=%s",
            caller,
            alias,
        )
        return {"status": "error", "message": str(e)}
    return {
        "status": "success",
        "alias": alias.strip().casefold(),
        "chat_id": cid,
        "chat_type": chat_type,
    }


async def telegram_save_last_forward_alias(
    alias: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Save the most recently forwarded message (in the caller's DM) as
    an alias. Requires `manage_aliases`. The 5-minute last-forward
    capture is held per session_id by the poller."""
    caller = _caller_user_id(tool_context)
    if not caller:
        return _missing_caller_error()
    err = await _require_capability(caller, "manage_aliases")
    if err is not None:
        return err

    state = getattr(tool_context, "state", None) if tool_context else None
    session_id: str = ""
    if state is not None:
        try:
            session_id = str(state.get("session_id", "") or "")
        except Exception:
            session_id = ""
    if not session_id:
        return {
            "status": "error",
            "message": "session_id missing from tool_context.state",
        }
    capture = telegram_store.pop_forward(session_id)
    if capture is None:
        return {
            "status": "error",
            "message": (
                "No recent forwarded message in this session (5-minute "
                "TTL elapsed or no forward seen). Forward the message "
                "again, then retry."
            ),
        }
    try:
        await telegram_store.save_alias(
            owner_user_id=caller,
            alias=alias,
            chat_id=capture.chat_id,
            chat_type=capture.chat_type,
            title=capture.title,
            username=capture.username,
        )
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.exception(
            "telegram_store.save_alias (last-forward) failed for owner=%s alias=%s",
            caller,
            alias,
        )
        return {"status": "error", "message": str(e)}
    return {
        "status": "success",
        "alias": alias.strip().casefold(),
        "chat_id": capture.chat_id,
        "chat_type": capture.chat_type,
        "title": capture.title,
        "username": capture.username,
    }


async def telegram_list_aliases(
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """List the caller's saved chat aliases. Self-scoped; no capability
    required."""
    caller = _caller_user_id(tool_context)
    if not caller:
        return _missing_caller_error()
    try:
        rows = await telegram_store.list_aliases(caller)
    except Exception as e:
        logger.exception(
            "telegram_store.list_aliases failed for owner=%s", caller
        )
        return {"status": "error", "message": str(e)}
    return {"status": "success", "aliases": rows, "owner_user_id": caller}


async def telegram_delete_alias(
    alias: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Remove a saved alias. Requires `manage_aliases`."""
    caller = _caller_user_id(tool_context)
    if not caller:
        return _missing_caller_error()
    err = await _require_capability(caller, "manage_aliases")
    if err is not None:
        return err
    try:
        deleted = await telegram_store.delete_alias(caller, alias)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.exception(
            "telegram_store.delete_alias failed for owner=%s alias=%s",
            caller,
            alias,
        )
        return {"status": "error", "message": str(e)}
    if not deleted:
        return {
            "status": "error",
            "message": f"alias '{alias}' not found for user {caller}",
        }
    return {"status": "success", "alias": alias.strip().casefold()}


async def telegram_resolve_alias(
    alias: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Resolve an alias to its chat_id. Self-scoped; no capability
    required."""
    caller = _caller_user_id(tool_context)
    if not caller:
        return _missing_caller_error()
    try:
        row = await telegram_store.resolve_alias(caller, alias)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.exception(
            "telegram_store.resolve_alias failed for owner=%s alias=%s",
            caller,
            alias,
        )
        return {"status": "error", "message": str(e)}
    if row is None:
        return {
            "status": "error",
            "message": f"alias '{alias}' not found for user {caller}",
        }
    return {"status": "success", **row}


# --------------------------------------------------------------------------- file forwarding (slice 7)


async def telegram_forward(
    file_ref: str,
    target: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Re-send a cached file (by file_ref) to a target chat.

    Uses the Telegram `file_id` reuse pattern — no byte upload. Falls
    back to `copyMessage` if Telegram rejects the cached `file_id`.
    `forward_files` capability required for non-DM targets.
    """
    caller = _caller_user_id(tool_context)
    if not caller:
        return _missing_caller_error()

    resolved = await _resolve_target(caller, target)
    if not resolved.get("ok"):
        return {"status": "error", "message": resolved["message"]}
    chat_id = resolved["chat_id"]
    chat_type = resolved.get("chat_type")

    if _is_non_dm(chat_id, chat_type):
        err = await _require_capability(caller, "forward_files")
        if err is not None:
            return err

    try:
        row = await telegram_store.get_file(caller, file_ref)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.exception(
            "telegram_store.get_file failed for owner=%s ref=%s",
            caller,
            file_ref,
        )
        return {"status": "error", "message": str(e)}
    if row is None:
        return {
            "status": "error",
            "message": (
                f"file_ref '{file_ref}' not in cache for user {caller} "
                f"(expired or never registered)."
            ),
        }

    adapter, err = _adapter_or_error()
    if err is not None:
        return err

    result = await adapter.send_media_strict(
        chat_id,
        data=None,
        mime_type=row.get("mime_type") or "",
        caption=row.get("caption") or "",
        file_id=row["file_id"],
        file_type=row["file_type"],
    )
    if result.get("ok"):
        return {
            "status": "success",
            "method": "file_id",
            "chat_id": result.get("chat_id", chat_id),
            "message_id": result.get("message_id"),
            "file_ref": row["file_ref"],
            "file_type": row["file_type"],
        }

    # ---- copyMessage fallback ----
    source_chat = row.get("source_chat_id")
    source_msg = row.get("source_message_id")
    primary_err = result.get("description", "")
    if source_chat is not None and source_msg is not None:
        logger.warning(
            "telegram_forward: file_id send failed for ref=%s (%s); "
            "trying copyMessage fallback",
            file_ref,
            primary_err,
        )
        copy_result = await adapter.copy_message_strict(
            target_id=chat_id,
            from_chat_id=int(source_chat),
            message_id=int(source_msg),
        )
        if copy_result.get("ok"):
            return {
                "status": "success",
                "method": "copyMessage",
                "chat_id": copy_result.get("chat_id", chat_id),
                "message_id": copy_result.get("message_id"),
                "file_ref": row["file_ref"],
                "file_type": row["file_type"],
                "primary_error": primary_err,
            }
        return {
            "status": "error",
            "message": (
                f"file_id rejected ({primary_err}); copyMessage fallback "
                f"also failed: {copy_result.get('description', '')}"
            ),
        }
    return {
        "status": "error",
        "message": (
            f"file_id rejected ({primary_err}); copyMessage fallback "
            f"unavailable (source_chat_id/message_id not recorded). "
            f"Re-upload the file."
        ),
    }


async def telegram_list_cached_files(
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """List the caller's cached outbound files. Self-scoped."""
    caller = _caller_user_id(tool_context)
    if not caller:
        return _missing_caller_error()
    try:
        rows = await telegram_store.list_files(caller)
    except Exception as e:
        logger.exception(
            "telegram_store.list_files failed for owner=%s", caller
        )
        return {"status": "error", "message": str(e)}
    return {"status": "success", "files": rows, "owner_user_id": caller}


# --------------------------------------------------------------------------- capability admin (slice 7)


async def telegram_grant_capability(
    user_id: str,
    capability: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Grant a capability to a user. ACT+TOTP-gated by
    `admin_tool_guardrail` (wired in slice 10). The tool body itself
    just performs the grant; the guardrail enforces admin identity +
    staging."""
    if not user_id or not capability:
        return {
            "status": "error",
            "message": "user_id and capability are required",
        }
    try:
        await capabilities.grant(user_id, capability)
        now_holds = await capabilities.list_for(user_id)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.exception(
            "capabilities.grant failed for user=%s cap=%s",
            user_id,
            capability,
        )
        return {"status": "error", "message": str(e)}
    return {
        "status": "success",
        "user_id": user_id,
        "capability": capability,
        "now_holds": now_holds,
    }


async def telegram_revoke_capability(
    user_id: str,
    capability: str,
    tool_context: ToolContext = None,
) -> dict[str, Any]:
    """Revoke a capability from a user. ACT+TOTP-gated by
    `admin_tool_guardrail` (wired in slice 10)."""
    if not user_id or not capability:
        return {
            "status": "error",
            "message": "user_id and capability are required",
        }
    try:
        await capabilities.revoke(user_id, capability)
        now_holds = await capabilities.list_for(user_id)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.exception(
            "capabilities.revoke failed for user=%s cap=%s",
            user_id,
            capability,
        )
        return {"status": "error", "message": str(e)}
    return {
        "status": "success",
        "user_id": user_id,
        "capability": capability,
        "now_holds": now_holds,
    }


async def telegram_list_capabilities(
    tool_context: ToolContext = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """List a user's capabilities. No-arg → self (no capability). With
    `user_id` arg → admin-only (proposal finding #7 — non-leaky
    multi-user). Every cross-user read attempt (granted or denied) is
    logged at INFO so the audit trail covers both paths."""
    caller = _caller_user_id(tool_context)
    if not caller:
        return _missing_caller_error()
    if user_id is None or user_id == caller:
        try:
            caps_list = await capabilities.list_for(caller)
        except Exception as e:
            logger.exception(
                "capabilities.list_for failed for user=%s", caller
            )
            return {"status": "error", "message": str(e)}
        return {
            "status": "success",
            "user_id": caller,
            "capabilities": caps_list,
        }
    if not _is_admin(caller):
        logger.info(
            "telegram_list_capabilities: cross-user read DENIED "
            "(non-admin caller=%s target=%s)",
            caller,
            user_id,
        )
        return {
            "status": "error",
            "message": (
                f"User {caller} cannot read capabilities of {user_id} "
                f"(admin-only for cross-user reads)."
            ),
        }
    logger.info(
        "telegram_list_capabilities: cross-user read ALLOWED "
        "(admin caller=%s target=%s)",
        caller,
        user_id,
    )
    try:
        caps_list = await capabilities.list_for(user_id)
    except Exception as e:
        logger.exception(
            "capabilities.list_for failed for user=%s", user_id
        )
        return {"status": "error", "message": str(e)}
    return {
        "status": "success",
        "user_id": user_id,
        "capabilities": caps_list,
    }
