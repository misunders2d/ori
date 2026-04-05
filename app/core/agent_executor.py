"""Platform-agnostic agent execution, session management, and context handling."""

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google import genai
from google.genai import types

from app.app_utils.models import get_model_name
from app.session_signals import get_pending_refresh


def _inject_metadata_header(
    text: str, timestamp: datetime, platform: str, state: dict | None = None
) -> str:
    """Build a metadata-prefixed message string, converting to user timezone if available.

    Args:
        text: The raw message text.
        timestamp: Message timestamp (assumed UTC if naive).
        platform: Platform identifier (e.g. 'telegram', 'cli').
        state: Optional session state dict; may contain 'user_preferences' with a
               'Timezone: Region/City' line for local time conversion.

    Returns:
        The message text prefixed with a ``[Metadata: ...]`` header line.
    """
    # Ensure the timestamp is timezone-aware (treat naive as UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    tz_label = "UTC"
    display_ts = timestamp

    # Attempt user-preferred timezone conversion
    if state:
        prefs = state.get("user_preferences", "") or ""
        for line in prefs.splitlines():
            line = line.strip()
            if line.lower().startswith("timezone:"):
                tz_name = line.split(":", 1)[1].strip()
                try:
                    user_tz = ZoneInfo(tz_name)
                    display_ts = timestamp.astimezone(user_tz)
                    tz_label = display_ts.strftime("%Z") or str(user_tz)
                except (ZoneInfoNotFoundError, KeyError):
                    pass  # fall back to UTC
                break

    ts_str = display_ts.strftime(f"%Y-%m-%d %H:%M:%S {tz_label}")
    header = f"[Metadata: {ts_str} | Platform: {platform}]"
    return f"{header}\n{text}"


@dataclass
class AgentResponse:
    """Structured response from the agent containing text and optional media."""
    text: str = ""
    media_items: list[dict] = field(default_factory=list)

    def __str__(self) -> str:
        """Backward-compatible string representation for callers that just need text."""
        return self.text

    def __contains__(self, item: str) -> bool:
        """Allow 'x in response' checks to work against the text body."""
        return item in self.text

logger = logging.getLogger(__name__)


async def _summarize_session(session) -> str:
    """Use Gemini to summarize session events into a compact context string."""
    texts = []
    for event in session.events:
        if event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    role = event.content.role or "unknown"
                    texts.append(f"{role}: {part.text}")
    if not texts:
        return ""
    # Take the last 80 exchanges max to stay within model limits
    conversation = "\n".join(texts[-80:])
    client = genai.Client()
    try:
        response = await client.aio.models.generate_content(
            model=get_model_name("session_summarizer"),
            contents=(
                "Summarize the following conversation into a concise context briefing. "
                "Preserve key facts, decisions, ongoing tasks, and user preferences. "
                "Keep it under 500 words.\n\n" + conversation
            ),
        )
        return response.text or ""
    except Exception:
        return ""


async def _perform_session_refresh(
    runner, user_id, session_id, mode: str, session=None
) -> str:
    """Core logic to wipe or summarize/reset a session."""
    if mode == "summarize":
        if session is None:
            session = await runner.session_service.get_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )

        summary = ""
        if session:
            summary = await _summarize_session(session)

        await runner.session_service.delete_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

        if summary:
            # Seed the new session with the summary as context
            await extract_agent_response(
                runner,
                user_id,
                session_id,
                f"[CONTEXT FROM PREVIOUS SESSION]\n{summary}\n[END CONTEXT]\n\n"
                "Acknowledge that you have received a summary of our previous "
                "conversation. Briefly confirm what you remember.",
            )
            return "New session started with context carried over."
        else:
            return "Could not generate a summary. Started a fresh session instead."
    else:
        # Fresh wipe
        await runner.session_service.delete_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        return "Fresh session started."


async def update_session_state(runner, user_id: str, session_id: str, state_delta: dict):
    """Natively injects state_delta into the ADK Session DB without invoking the graph."""
    from google.adk.events.event import Event, EventActions

    logger.info(f"DEBUG: update_session_state(user_id={user_id}, session_id={session_id}, state_delta={state_delta})")

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


async def extract_agent_response(
    runner, user_id: str, session_id: str, message: str | types.Content, actual_caller_id: str = None
) -> AgentResponse:
    """Run the ADK runner and yield agent responses with optional media attachments."""
    try:
        from google.adk.events.event import Event, EventActions
    except ImportError:
        import sys
        Event = sys.modules['google.adk.events.event'].Event
        EventActions = sys.modules['google.adk.events.event'].EventActions

    logger.info(f"DEBUG: extract_agent_response(user_id={user_id}, session_id={session_id}, actual_caller_id={actual_caller_id})")

    if actual_caller_id:
        await update_session_state(
            runner=runner,
            session_id=session_id,
            user_id=user_id,
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

    # Prune session if it exceeds 200 events (approx 100 exchanges)
    if session and len(session.events) > 200:
        logger.info("Pruning large session %s", session_id)
        # We keep the last 100 events to ensure we don't lose immediate context
        session.events = session.events[-100:]

    MAX_RETRIES = 3

    # Prepare message_arg
    if isinstance(message, str):
        message_arg = types.Content(role="user", parts=[types.Part.from_text(text=message)])
    else:
        message_arg = message

    agent_text_parts = []
    media_items = []
    
    # Track latest tool results to provide better feedback if the model is silent
    latest_tool_results = []

    for attempt in range(1 + MAX_RETRIES):
        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=message_arg,
            ):
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            agent_text_parts.append(part.text)
                        # Capture tool results for fallback feedback
                        elif hasattr(part, "function_response") and part.function_response:
                            res = part.function_response.response
                            if isinstance(res, dict) and "message" in res:
                                latest_tool_results.append(res["message"])
                            elif isinstance(res, dict) and "status" in res:
                                latest_tool_results.append(f"Status: {res['status']}")
                        # Capture inline binary data (images, audio, etc.)
                        elif hasattr(part, "inline_data") and part.inline_data:
                            media_items.append({
                                "data": part.inline_data.data,
                                "mime_type": part.inline_data.mime_type or "application/octet-stream",
                            })

            break  # success
        except Exception as exc:
            error_msg = str(exc).split("\n")[0] if str(exc) else type(exc).__name__
            logger.warning(
                "Agent error (attempt %d/%d) for user %s: %s",
                attempt + 1,
                1 + MAX_RETRIES,
                user_id,
                error_msg,
            )

            # Catch readonly database — self-heal by nuking the session DB
            if "readonly database" in error_msg.lower():
                logger.error("Session DB is readonly — attempting self-heal.")
                try:
                    import os, sqlite3 as _sqlite3
                    db_path = os.path.abspath("./data/ori-sessions.db")
                    for suffix in ("", "-wal", "-shm", "-journal"):
                        try:
                            os.remove(db_path + suffix)
                        except FileNotFoundError:
                            pass
                    # Force runner recreation on next message
                    import run_bot
                    run_bot._global_runner = None
                    logger.info("Session DB deleted and runner reset — next message will recover.")
                except Exception as heal_err:
                    logger.error("Self-heal failed: %s", heal_err)
                return AgentResponse(
                    text="⚠️ **Database Error**\n\n"
                    "I hit a database issue but have auto-repaired it. "
                    "Please resend your message."
                )

            # Catch rate limit / quota errors — back off and retry via the loop
            if (
                "429" in error_msg
                or "RESOURCE_EXHAUSTED" in error_msg
                or "QuotaExceeded" in error_msg
            ):
                import asyncio as _asyncio
                _delay = min(30 * (attempt + 1), 60)
                logger.warning("API rate limit hit. Backing off %ds before retry...", _delay)
                await _asyncio.sleep(_delay)
                if attempt >= MAX_RETRIES:
                    return AgentResponse(
                        text="⚠️ **Rate Limit Exceeded**\n\n"
                        "I retried after backing off but the API quota is still exhausted. "
                        "Please wait a few minutes and try again."
                    )
                continue

            # Catch token limit / context window errors
            if "token count exceeds" in error_msg.lower() or "400" in error_msg:
                logger.error(
                    "Context limit reached for session %s: %s", session_id, error_msg
                )
                return AgentResponse(
                    text="⚠️ **Context Limit Reached**\n\n"
                    "The conversation has become too large for me to process. "
                    "Send /reset to start a fresh session."
                )

            if attempt < MAX_RETRIES:
                agent_text_parts.clear()
                message_arg = types.Content(
                    role="user",
                    parts=[
                        types.Part.from_text(
                            text=(
                                f"Your previous action failed with this error: {error_msg}\n"
                                "Analyze what went wrong and retry my original request. "
                                "If a tool caused the error, do NOT use it again."
                            )
                        )
                    ],
                )
                continue
            return AgentResponse(
                text=f"Agent error after {1 + MAX_RETRIES} attempts. Last error: {error_msg}"
            )

    if not agent_text_parts:
        final_text = "I processed your request but have no response to show."
    else:
        final_text = "\n".join(agent_text_parts)

    # Check for manual session refresh signal
    refresh_mode = get_pending_refresh(session_id)
    if refresh_mode:
        logger.info(
            "Manual session refresh (%s) triggered for %s", refresh_mode, session_id
        )
        refresh_msg = await _perform_session_refresh(
            runner, user_id, session_id, refresh_mode
        )
        final_text += f"\n\n--- SESSION REFRESHED ---\n{refresh_msg}"

    return AgentResponse(text=final_text, media_items=media_items)


async def process_message_for_context(runner, user_id: str, session_id: str, message: str | types.Content):
    """Silently add a message as context to the session without triggering the agent."""
    try:
        from google.adk.events.event import Event
    except ImportError:
        import sys
        Event = sys.modules['google.adk.events.event'].Event

    session = await runner.session_service.get_session(
        app_name=runner.app_name, user_id=user_id, session_id=session_id
    )
    if session is None:
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

    # Hard truncate if it exceeds 100 messages (approx 200 events)
    if session and len(session.events) > 200:
        session.events = session.events[-100:]

    if isinstance(message, str):
        content = types.Content(role="user", parts=[types.Part.from_text(text=message)])
    else:
        content = message

    event = Event(
        id=str(uuid.uuid4()),
        author=user_id,
        timestamp=time.time(),
        content=content
    )
    await runner.session_service.append_event(session, event)
