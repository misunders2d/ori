"""Runner wrapper — pumps events from `runner.run_async` into a clean
AgentResponse, with retry/backoff on 429s, session pruning, readonly-DB
self-heal, and post-turn session-refresh signal handling.

Replaces the legacy `app/core/agent_executor.py` (deleted in this branch).
Same responsibilities, cleaner imports (app.runtime.* / app.util.*),
durable plan-store integration on session refresh.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google import genai
from google.genai import types

from app.runtime.session_signals import get_pending_refresh
from app.util.models import get_model_name

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AgentResponse — what transport pollers receive from extract_agent_response.
# ---------------------------------------------------------------------------

@dataclass
class AgentResponse:
    """Structured response from the agent: final text + optional media."""
    text: str = ""
    media_items: list[dict] = field(default_factory=list)

    def __str__(self) -> str:
        return self.text

    def __contains__(self, item: str) -> bool:
        return item in self.text


# ---------------------------------------------------------------------------
# Metadata header injection (UME — User Message Envelope)
# ---------------------------------------------------------------------------

def _inject_metadata_header(
    text: str,
    timestamp: datetime,
    platform: str,
    state: dict | None = None,
) -> str:
    """Prefix a user message with `[Metadata: <ts> | Platform: <plat>]`.

    Converts `timestamp` to the user's preferred timezone if their
    `user_preferences` state contains a `Timezone: Region/City` line.
    """
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    tz_label = "UTC"
    display_ts = timestamp
    if state:
        prefs = state.get("user_preferences", "") or ""
        if isinstance(prefs, dict):
            # Newer state format: dict of preferences. Try common keys.
            tz_name = prefs.get("timezone") or prefs.get("Timezone")
            if tz_name:
                try:
                    user_tz = ZoneInfo(tz_name)
                    display_ts = timestamp.astimezone(user_tz)
                    tz_label = display_ts.strftime("%Z") or str(user_tz)
                except (ZoneInfoNotFoundError, KeyError):
                    pass
        elif isinstance(prefs, str):
            for line in prefs.splitlines():
                line = line.strip()
                if line.lower().startswith("timezone:"):
                    tz_name = line.split(":", 1)[1].strip()
                    try:
                        user_tz = ZoneInfo(tz_name)
                        display_ts = timestamp.astimezone(user_tz)
                        tz_label = display_ts.strftime("%Z") or str(user_tz)
                    except (ZoneInfoNotFoundError, KeyError):
                        pass
                    break
    ts_str = display_ts.strftime(f"%Y-%m-%d %H:%M:%S {tz_label}")
    return f"[Metadata: {ts_str} | Platform: {platform}]\n{text}"


# ---------------------------------------------------------------------------
# Session refresh — explicit /reset, or post-turn signal from a tool.
# ---------------------------------------------------------------------------

async def _summarize_session(session) -> str:
    """Use the configured summarizer to compress session events into context."""
    texts: list[str] = []
    for event in session.events or []:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    role = (event.content.role or "unknown")
                    texts.append(f"{role}: {part.text}")
    if not texts:
        return ""
    conversation = "\n".join(texts[-80:])
    client = genai.Client()
    try:
        resp = await client.aio.models.generate_content(
            model=get_model_name("summarizer"),
            contents=(
                "Summarize the following conversation into a concise context "
                "briefing. Preserve key facts, decisions, ongoing tasks, and "
                "user preferences. Keep it under 500 words.\n\n" + conversation
            ),
        )
        return resp.text or ""
    except Exception:
        return ""


async def _perform_session_refresh(
    runner,
    user_id: str,
    session_id: str,
    mode: str,
    session=None,
) -> str:
    """Wipe (`mode='fresh'`) or summarize-then-wipe (`mode='summarize'`)."""
    if mode == "summarize":
        if session is None:
            session = await runner.session_service.get_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )
        summary = await _summarize_session(session) if session else ""
        await runner.session_service.delete_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        if summary:
            await extract_agent_response(
                runner, user_id, session_id,
                f"[CONTEXT FROM PREVIOUS SESSION]\n{summary}\n[END CONTEXT]\n\n"
                "Acknowledge that you have received a summary of our previous "
                "conversation. Briefly confirm what you remember.",
            )
            return "New session started with context carried over."
        return "Could not generate a summary. Started a fresh session instead."
    # Fresh wipe
    await runner.session_service.delete_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    # Abandon any active plan in the durable store — the previous session's
    # plan no longer applies to the next conversation.
    try:
        from app.runtime.plan_storage import abandon_plan
        await abandon_plan(session_id)
    except Exception:
        logger.debug("session refresh: abandon_plan skipped")
    return "Fresh session started."


# ---------------------------------------------------------------------------
# State delta injection (used to inject actual_caller_id into group sessions)
# ---------------------------------------------------------------------------

async def update_session_state(
    runner,
    user_id: str,
    session_id: str,
    state_delta: dict,
) -> None:
    """Append a synthetic event whose only effect is a state_delta."""
    from google.adk.events.event import Event, EventActions

    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    if not session:
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
    await runner.session_service.append_event(
        session=session,
        event=Event(
            id=str(uuid.uuid4()),
            author="__system__",
            content=None,
            actions=EventActions(state_delta=state_delta),
        ),
    )


# ---------------------------------------------------------------------------
# extract_agent_response — the big one.
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_PRUNE_THRESHOLD = 200       # events
_PRUNE_KEEP_TAIL = 100       # events kept after prune


async def extract_agent_response(
    runner,
    user_id: str,
    session_id: str,
    message: str | types.Content,
    actual_caller_id: str | None = None,
) -> AgentResponse:
    """Run the runner and synthesize a final user-facing AgentResponse.

    Responsibilities:
    - Inject `actual_caller_id` into session state if provided (group chats).
    - Get-or-create the session.
    - Prune sessions with >200 events (keep last 100).
    - Pump events from runner.run_async, collecting text + media.
    - Retry on 429 / RESOURCE_EXHAUSTED with exponential backoff.
    - Self-heal a readonly session DB.
    - Surface a context-limit error when the session is too large.
    - After the turn: check for a pending session-refresh signal and
      run it if set.
    """
    if actual_caller_id:
        await update_session_state(
            runner=runner,
            user_id=user_id,
            session_id=session_id,
            state_delta={"user_id": actual_caller_id},
        )

    try:
        session = await runner.session_service.get_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        if session is None:
            session = await runner.session_service.create_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )
    except Exception:
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

    # Prune if too large.
    if session and len(session.events) > _PRUNE_THRESHOLD:
        logger.info("Pruning large session %s (%d events)", session_id, len(session.events))
        session.events = session.events[-_PRUNE_KEEP_TAIL:]

    # Normalize message into Content.
    if isinstance(message, str):
        message_arg: types.Content = types.Content(
            role="user", parts=[types.Part.from_text(text=message)]
        )
    else:
        message_arg = message

    agent_text_parts: list[str] = []
    media_items: list[dict] = []
    latest_tool_results: list[str] = []

    for attempt in range(1 + _MAX_RETRIES):
        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=message_arg,
            ):
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if getattr(part, "text", None):
                            agent_text_parts.append(part.text)
                        elif getattr(part, "function_response", None):
                            res = part.function_response.response
                            if isinstance(res, dict):
                                if "message" in res:
                                    latest_tool_results.append(str(res["message"]))
                                elif "status" in res:
                                    latest_tool_results.append(f"Status: {res['status']}")
                                # Tool returned a file path (image gen, chart
                                # export, etc.) — read it and surface as a
                                # media attachment so the transport delivers
                                # the actual image/file rather than narrating
                                # the path.
                                fp = res.get("file_path")
                                if fp and os.path.isfile(fp):
                                    mime, _ = mimetypes.guess_type(fp)
                                    with open(fp, "rb") as f:
                                        media_items.append({
                                            "data": f.read(),
                                            "mime_type": mime or "application/octet-stream",
                                        })
                        elif getattr(part, "inline_data", None):
                            media_items.append({
                                "data": part.inline_data.data,
                                "mime_type": part.inline_data.mime_type or "application/octet-stream",
                            })
            break  # success
        except Exception as exc:
            error_msg = (str(exc).split("\n")[0] if str(exc) else type(exc).__name__)
            logger.warning(
                "Agent error (attempt %d/%d) for user %s: %s",
                attempt + 1, 1 + _MAX_RETRIES, user_id, error_msg,
            )

            # Readonly DB → self-heal.
            if "readonly database" in error_msg.lower():
                logger.error("Session DB is readonly — attempting self-heal.")
                try:
                    db_path = os.path.abspath("./data/ori-sessions.db")
                    for suffix in ("", "-wal", "-shm", "-journal"):
                        try:
                            os.remove(db_path + suffix)
                        except FileNotFoundError:
                            pass
                    import run_bot
                    run_bot._global_runner = None
                except Exception as heal_err:
                    logger.error("Self-heal failed: %s", heal_err)
                return AgentResponse(text=(
                    "⚠️ Database Error\n\n"
                    "I hit a database issue but have auto-repaired it. "
                    "Please resend your message."
                ))

            # Rate limit → exponential backoff retry.
            if any(s in error_msg for s in ("429", "RESOURCE_EXHAUSTED", "QuotaExceeded")):
                import asyncio
                delay = min(30 * (attempt + 1), 60)
                logger.warning("Rate limit. Backing off %ds before retry...", delay)
                await asyncio.sleep(delay)
                if attempt >= _MAX_RETRIES:
                    return AgentResponse(text=(
                        "⚠️ Rate Limit Exceeded\n\n"
                        "I retried after backing off but the API quota is still "
                        "exhausted. Please wait a few minutes and try again."
                    ))
                continue

            # Token / context window limit.
            if any(s in error_msg.lower() for s in ("token count exceeds", "input too large")):
                logger.error("Context limit for session %s: %s", session_id, error_msg)
                return AgentResponse(text=(
                    "⚠️ Context Limit Reached\n\n"
                    "The conversation has become too large for me to process. "
                    "Send /reset to start a fresh session."
                ))

            # Other error — retry by sending an analysis prompt.
            if attempt < _MAX_RETRIES:
                agent_text_parts.clear()
                message_arg = types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=(
                        f"Your previous action failed with this error: {error_msg}\n"
                        "Analyze what went wrong and retry my original request. "
                        "If a tool caused the error, do NOT use it again."
                    ))],
                )
                continue
            return AgentResponse(
                text=f"Agent error after {1 + _MAX_RETRIES} attempts. Last error: {error_msg}"
            )

    final_text = "\n".join(agent_text_parts) if agent_text_parts else (
        "I processed your request but have no response to show."
    )

    # Post-turn refresh signal handling.
    refresh_mode = get_pending_refresh(session_id)
    if refresh_mode:
        logger.info("Manual session refresh (%s) for %s", refresh_mode, session_id)
        refresh_msg = await _perform_session_refresh(
            runner, user_id, session_id, refresh_mode
        )
        final_text += f"\n\n--- SESSION REFRESHED ---\n{refresh_msg}"

    return AgentResponse(text=final_text, media_items=media_items)


async def process_message_for_context(
    runner,
    user_id: str,
    session_id: str,
    message: str | types.Content,
) -> None:
    """Silently append a message to session history without invoking the agent.

    Used by group transports when a message is sent without bot mention —
    we save the context so a later mention has the conversational thread.
    """
    from google.adk.events.event import Event

    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    if session is None:
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

    if session and len(session.events) > _PRUNE_THRESHOLD:
        session.events = session.events[-_PRUNE_KEEP_TAIL:]

    if isinstance(message, str):
        content = types.Content(role="user", parts=[types.Part.from_text(text=message)])
    else:
        content = message

    await runner.session_service.append_event(
        session,
        Event(
            id=str(uuid.uuid4()),
            author=user_id,
            timestamp=time.time(),
            content=content,
        ),
    )
