"""Platform-agnostic agent execution, session management, and context handling."""

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

from app.app_utils.models import get_model_name
from app.session_signals import get_pending_refresh


def _inject_metadata_header(
    text: str,
    timestamp: datetime,
    platform: str,
    state: dict | None = None,
    thread_id: str | None = None,
) -> str:
    """Build a metadata-prefixed message string, converting to user timezone if available.

    Args:
        text: The raw message text.
        timestamp: Message timestamp (assumed UTC if naive).
        platform: Platform identifier (e.g. 'telegram', 'cli').
        state: Optional session state dict; may contain 'user_preferences' with a
               'Timezone: Region/City' line for local time conversion.
        thread_id: Optional thread identifier. For Slack this is the
            ``thread_ts`` (parent message timestamp). When supplied,
            the agent sees ``Thread: <ts>`` in the metadata header,
            so it can reason about which thread it's responding in
            and pass the same value to ``slack_post_message`` /
            ``slack_read_replies`` calls.

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
    parts = [f"Metadata: {ts_str}", f"Platform: {platform}"]
    if thread_id:
        parts.append(f"Thread: {thread_id}")
    header = "[" + " | ".join(parts) + "]"
    return f"{header}\n{text}"


@dataclass
class AgentResponse:
    """Structured response from the agent containing text and optional media.

    `error` is set when the agent bailed on a terminal condition (context
    limit exceeded, rate-limit-after-retries, readonly-DB self-heal, etc).
    Callers that loop on agent invocation MUST check this and break out
    instead of retrying — the underlying session is poisoned and further
    calls will fail the same way, burning quota.

    `media_items` is a list of dicts with the following keys:

    - ``data`` (``bytes``): the file payload.
    - ``mime_type`` (``str``): MIME type, e.g. ``image/png``.
    - ``file_path`` (``str | None``): the on-disk path the bytes came
      from when available (function-response ``file_path`` field or the
      ``__contract_file:`` marker on inline_data). ``None`` for user
      uploads or any payload whose origin we don't track. Slice 6 reads
      this so the Telegram transport can auto-derive a ``file_ref`` for
      the outbound-files cache; consumers that don't need it can ignore.
    """
    text: str = ""
    media_items: list[dict] = field(default_factory=list)
    error: str | None = None

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

    # Clean up scratchpads and plans for this session
    from app.tools.scratchpad import cleanup_session_scratchpads
    cleanup_session_scratchpads(session_id)
    try:
        plan_path = os.path.join(os.path.abspath("./tmp/plans"), f"{session_id}.json")
        if os.path.exists(plan_path):
            os.remove(plan_path)
    except Exception:
        pass

    if mode != "summarize":
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

    # Prune session if it exceeds 500 events — persist to DB via delete+recreate
    if session and len(session.events) > 500:
        logger.info("Pruning session %s (%d events)", session_id, len(session.events))
        preserved_state = dict(session.state) if session.state else {}
        await runner.session_service.delete_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id,
            state=preserved_state,
        )

    MAX_RETRIES = 3

    # Embed actual caller ID as a hidden tag in the message so state_setter
    # can extract it without fabricating synthetic __system__ events.
    if actual_caller_id:
        caller_tag = f"[__caller_id:{actual_caller_id}__]"
        if isinstance(message, str):
            message = caller_tag + message
        elif message.parts:
            # Prepend to the first text part
            for part in message.parts:
                if hasattr(part, "text") and part.text:
                    part.text = caller_tag + part.text
                    break

    # Prepare message_arg
    if isinstance(message, str):
        message_arg = types.Content(role="user", parts=[types.Part.from_text(text=message)])
    else:
        message_arg = message

    agent_text_parts = []
    media_items = []
    # Dedupe attachments: the file_attachment_inject after_model callback
    # appends inline_data Parts (with a display_name marker) so the A2A
    # converter ships chart/image files as FileParts. The same files are
    # also reachable via the function_response.file_path branch below.
    # Without dedup, Slack/Telegram would receive each file twice.
    _attached_file_paths: set[str] = set()
    _FILE_ATTACHMENT_MARKER = "__contract_file:"
    
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
                            # Skip reasoning / thinking parts. ADK's LiteLlm
                            # wrapper normalises Anthropic ``thinking_blocks``
                            # (and any provider's reasoning content) into
                            # ``Part(text=..., thought=True)``. Those are
                            # internal to the model and must not surface in
                            # user-facing channels (Slack, Telegram, A2A).
                            # Gemini's own thinking is suppressed earlier in
                            # ``state_setter`` via ``thinking_config = None``;
                            # this filter is the LiteLlm-path counterpart and
                            # acts as a belt-and-suspenders guard for Gemini
                            # too.
                            # Use `is True` rather than truthiness so a
                            # MagicMock-based test part (whose .thought is a
                            # MagicMock and therefore truthy) doesn't get
                            # filtered. Anthropic/ADK both set the literal
                            # `True`.
                            if getattr(part, "thought", None) is True:
                                continue
                            agent_text_parts.append(part.text)
                        # Capture tool results for fallback feedback + file attachments
                        elif hasattr(part, "function_response") and part.function_response:
                            res = part.function_response.response
                            if isinstance(res, dict) and "message" in res:
                                latest_tool_results.append(res["message"])
                            elif isinstance(res, dict) and "status" in res:
                                latest_tool_results.append(f"Status: {res['status']}")
                            # Detect file paths from tool responses (charts, exports).
                            # Skip if an inline_data Part with the same path
                            # was already added by file_attachment_inject —
                            # that path is the A2A-friendly route and dedupe
                            # prevents Slack/Telegram from receiving the same
                            # file twice.
                            if isinstance(res, dict) and res.get("file_path"):
                                fp = res["file_path"]
                                fp_abs = os.path.abspath(fp) if fp else fp
                                if fp_abs in _attached_file_paths:
                                    pass  # already attached via inline_data
                                elif fp and os.path.isfile(fp):
                                    mime, _ = mimetypes.guess_type(fp)
                                    with open(fp, "rb") as f:
                                        media_items.append({
                                            "data": f.read(),
                                            "mime_type": mime or "application/octet-stream",
                                            # slice 5: thread the path so the
                                            # transport layer can auto-derive a
                                            # file_ref for the outbound-files
                                            # cache.
                                            "file_path": fp_abs,
                                        })
                                    _attached_file_paths.add(fp_abs)
                        # Capture inline binary data (images, audio, etc.)
                        elif hasattr(part, "inline_data") and part.inline_data:
                            display_name = getattr(part.inline_data, "display_name", "") or ""
                            marked_path = ""
                            if display_name.startswith(_FILE_ATTACHMENT_MARKER):
                                marked_path = os.path.abspath(
                                    display_name[len(_FILE_ATTACHMENT_MARKER):]
                                )
                                if marked_path in _attached_file_paths:
                                    continue  # already attached via function_response branch
                            media_items.append({
                                "data": part.inline_data.data,
                                "mime_type": part.inline_data.mime_type or "application/octet-stream",
                                # slice 5: thread the marker path (if any) so
                                # the transport layer can auto-derive a
                                # file_ref. Empty when the inline_data came
                                # from a user upload, not file_attachment_inject.
                                "file_path": marked_path or None,
                            })
                            if marked_path:
                                _attached_file_paths.add(marked_path)

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
                    import sqlite3 as _sqlite3
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
                    "Please resend your message.",
                    error="readonly_database",
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
                        "Please wait a few minutes and try again.",
                        error="rate_limit_exhausted",
                    )
                continue

            # Catch token limit / context window errors (specific patterns only)
            _context_patterns = [
                "token count exceeds",
                "request payload size exceeds",
                "context length exceeded",
                "maximum context length",
                "content too large",
            ]
            if any(p in error_msg.lower() for p in _context_patterns):
                logger.error(
                    "Context limit reached for session %s: %s", session_id, error_msg
                )
                return AgentResponse(
                    text="⚠️ **Context Limit Reached**\n\n"
                    "The conversation has become too large for me to process. "
                    "Send /reset to start a fresh session.",
                    error="context_limit",
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
                text=f"Agent error after {1 + MAX_RETRIES} attempts. Last error: {error_msg}",
                error="max_retries_exceeded",
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

    # Prune if session exceeds 50 events — persist to DB via delete+recreate
    if session and len(session.events) > 50:
        logger.info("Pruning context session %s (%d events)", session_id, len(session.events))
        preserved_state = dict(session.state) if session.state else {}
        await runner.session_service.delete_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id,
            state=preserved_state,
        )

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


async def list_invocation_tool_calls(
    runner,
    user_id: str,
    session_id: str,
    since_epoch: float,
) -> list[dict]:
    """Return tool calls + responses recorded in the session since
    ``since_epoch`` (Unix float).

    Used by ``app.tasks.run_scheduled_task``'s fabrication detector to
    decide whether a scheduled fire actually invoked the tools its
    prompt requires. Read-only — never mutates state. Logs CRITICAL and
    re-raises on read failure (Law 6: no silent empty list).

    ADK Event timestamps are float Unix epoch values (set via
    ``time.time()`` when events are appended — see this module's
    ``update_session_state`` and the legacy paths above). Callers must
    therefore pass a float epoch; comparing against a ``datetime``
    would raise ``TypeError`` on every event.

    Returns a list of dicts, each shaped:
        {"name": str,
         "args_keys": list[str],            # function_call only
         "response_status": str | None,     # function_response only
         "response_message": str | None,    # function_response only
         "timestamp": float}
    Function calls and function responses appear as separate entries so
    callers can correlate them (or count distinct call sites).
    """
    try:
        session = await runner.session_service.get_session(
            app_name=runner.app_name,
            user_id=user_id,
            session_id=session_id,
        )
    except Exception as exc:
        logger.critical(
            "list_invocation_tool_calls: failed to load session %r for "
            "user %r: %s",
            session_id, user_id, exc, exc_info=True,
        )
        raise

    if not session or not session.events:
        logger.warning(
            "list_invocation_tool_calls: session %r has no events — "
            "scheduled fire produced zero tool activity",
            session_id,
        )
        return []

    out: list[dict] = []
    for ev in session.events:
        ts = getattr(ev, "timestamp", None)
        # ADK Event.timestamp is float Unix epoch. Guard for None and
        # non-numeric — some synthetic events may lack a timestamp.
        if not isinstance(ts, (int, float)) or ts < since_epoch:
            continue
        content = getattr(ev, "content", None)
        parts = getattr(content, "parts", None) if content is not None else None
        if not parts:
            continue
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None):
                args = getattr(fc, "args", {}) or {}
                out.append({
                    "name": fc.name,
                    "args_keys": list(args.keys()) if hasattr(args, "keys") else [],
                    "response_status": None,
                    "response_message": None,
                    "timestamp": float(ts),
                })
            fr = getattr(part, "function_response", None)
            if fr and getattr(fr, "name", None):
                resp = getattr(fr, "response", None)
                status = None
                message = None
                if isinstance(resp, dict):
                    status = resp.get("status")
                    raw_msg = resp.get("message")
                    if isinstance(raw_msg, str):
                        message = raw_msg
                out.append({
                    "name": fr.name,
                    "args_keys": [],
                    "response_status": status,
                    "response_message": message,
                    "timestamp": float(ts),
                })
    return out
