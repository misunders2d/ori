"""Mirror contract emit results into the receiving channel's chat
session so the bot sees its own scheduled output on the next turn.

Why this exists
---------------

The bot's conversation history is keyed by ``session_id``, which the
Slack and Telegram pollers derive from the channel / chat id:

  * Slack  → ``sl_<channel_id>`` (one session per channel; all users
              in a channel share the same session).
  * Telegram → ``tg_<chat_id>`` (one session per DM or group chat).

Human posts in those channels reach the session via the poller calling
``process_message_for_context``. The bot's OWN posts via the Slack /
Telegram API come back through the poller with a bot-message subtype
and are explicitly DROPPED (``slack_poller.py:277-278``) — otherwise
we'd loop on our own output.

Legacy scheduled tasks plug this gap with ``tasks._inject_into_session``
(``app/tasks.py:550``), which appends a model-role event to the
``origin_session_id`` recorded on the notify dict.

Contracts skipped the gap entirely. ``run_contract_fire`` →
``execute_contract`` → emit adapter → done. No session ever heard
about the post. Result: the bot would post an AI-Pilot blurb to
``#ai_in_mellanni`` at 18:00, the user would ask "what did you
post earlier today?" in DM five minutes later, and the bot would say
"I haven't posted anything" because its DM session had zero record.

This module fills the gap. After every successful emit, the worker
calls ``mirror_emit_to_session(adapter, args, summary)`` here. We:

  1. Resolve the emit's target → session_id using the same
     ``make_session_id`` logic the pollers use.
  2. Look up the runner singleton (the same one Slack/Telegram pollers
     use), open or create the session, and append a model-role event
     carrying the summary.

If no runner is running (bot in headless / unit-test mode) or the
target can't be resolved, we silently no-op — the contract fire
must NOT fail because of a mirror miss.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Slack / Telegram session-id prefixes — match
# ``app/core/transport_slack.py:make_session_id`` and
# ``interfaces/telegram_poller.py:make_session_id``. Kept in sync
# manually rather than importing the adapter classes (which require
# httpx clients and tokens to construct), so this module stays
# import-cheap and works in the contract worker process without the
# poller's wiring.
_SLACK_SESSION_PREFIX = "sl_"
_TELEGRAM_SESSION_PREFIX = "tg_"


def resolve_emit_target_session(adapter: str, args: dict[str, Any]) -> Optional[str]:
    """Map an emit adapter + args dict to the session id of the
    channel / chat that will receive the message. Returns None for
    adapters whose output doesn't land in a user-facing chat session
    (memory_update, sheet_append, drive_doc_fill — those write
    persistent stores, not chats).

    Adapters explicitly handled:

      * ``slack_post`` — ``args.channel`` is the Slack channel ID (or
        ``#name`` form). The session is ``sl_<channel_id>``. The
        ``#name`` form can't be cleanly mapped without a Slack API
        round-trip, so we leave that to the bot's own ingest path
        and return None — those posts won't be mirrored to a chat
        session. (Almost every production contract uses raw ids.)
      * ``telegram_dm`` — ``args.user_id`` is the chat id for a DM
        (Telegram private chats: chat_id == user_id). Session is
        ``tg_<chat_id>``.

    Unknown adapters return None so they don't show up as ghost
    sessions if a future adapter targets something non-chat (memory,
    docs, sheets).
    """
    if adapter == "slack_post":
        channel = args.get("channel") or args.get("channel_id") or ""
        channel = str(channel).strip()
        if not channel or channel.startswith("#"):
            return None
        return f"{_SLACK_SESSION_PREFIX}{channel}"
    if adapter == "telegram_dm":
        user_id = args.get("user_id") or args.get("chat_id") or ""
        user_id = str(user_id).strip()
        if not user_id:
            return None
        # Telegram private chats: chat_id == user_id. We use the user
        # id as-is for the session prefix to match what the Telegram
        # adapter does at ``make_session_id``.
        return f"{_TELEGRAM_SESSION_PREFIX}{user_id}"
    return None


async def mirror_emit_to_session(
    adapter: str,
    args: dict[str, Any],
    text: str,
    *,
    contract_id: str | None = None,
    author: str = "contract_runner",
) -> bool:
    """Append a model-role event carrying ``text`` to the session that
    received the emit, so the bot's next turn in that channel sees
    its own scheduled output as part of the conversation history.

    Returns ``True`` on a clean append, ``False`` when the mirror was
    a no-op (no resolvable target, no runner, runner not yet ready,
    session not pre-existing). NEVER raises — the contract fire
    succeeded, and a mirror miss must not cascade into a fire
    failure.

    Args:
        adapter: registered emit adapter name (``slack_post``,
            ``telegram_dm``, ...).
        args: rendered emit args (post-template).
        text: the message body the user / channel just received,
            verbatim where possible. The bot will see this as a
            model-authored event tagged with ``contract_runner`` as
            the author.
        contract_id: optional contract id for log correlation.
        author: ADK event author field. Defaults to
            ``contract_runner`` so the bot can distinguish
            contract-driven turns from human turns or its own
            interactive replies.
    """
    target_session = resolve_emit_target_session(adapter, args)
    if target_session is None:
        return False

    try:
        from run_bot import get_runner

        runner = get_runner()
    except Exception as e:
        logger.debug("audit_mirror: runner not available (%s)", e)
        return False
    if runner is None:
        return False

    try:
        from google.adk.events.event import Event
        from google.genai import types as _types
    except Exception as e:
        logger.warning("audit_mirror: ADK import failed: %s", e)
        return False

    try:
        # Channel sessions follow the convention that ADK user_id ==
        # session_id (mirrors the poller pattern in
        # ``slack_poller.py:296`` and ``telegram_poller.py``). We
        # match it so the bot picks up the same session it answers
        # under for human messages.
        session = await runner.session_service.get_session(
            app_name=runner.app_name,
            user_id=target_session,
            session_id=target_session,
        )
        if session is None:
            # No human has chatted in this channel yet — the bot's
            # next interactive turn there will create the session and
            # see the post via the poller path. Mirror is a no-op.
            return False

        content = _types.Content(
            role="model",
            parts=[_types.Part.from_text(text=text)],
        )
        event = Event(
            id=str(uuid.uuid4()),
            author=author,
            timestamp=time.time(),
            content=content,
        )
        await runner.session_service.append_event(session, event)
        logger.info(
            "audit_mirror: appended contract emit (%s, contract=%s) to session %s",
            adapter,
            contract_id,
            target_session,
        )
        return True
    except Exception as e:
        logger.warning(
            "audit_mirror: failed to append to session %s: %s",
            target_session,
            e,
        )
        return False
