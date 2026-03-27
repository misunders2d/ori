"""Platform-agnostic agent execution, session management, and context handling."""

import logging
import time
import uuid
from dataclasses import dataclass, field

from google import genai
from google.genai import types

from app.session_signals import get_pending_refresh


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
    response = await client.aio.models.generate_content(
        model="gemini-3-flash-preview",
        contents=(
            "Summarize the following conversation into a concise context briefing. "
            "Preserve key facts, decisions, ongoing tasks, and user preferences. "
            "Keep it under 500 words.\n\n" + conversation
        ),
    )
    return response.text or ""


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
            await runner.session_service.create_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )

    except Exception:
        await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

    MAX_RETRIES = 2

    import re
    message_str = ""
    if isinstance(message, str):
        message_str = message
    else:
        # Extract text from types.Content
        parts = []
        for p in (getattr(message, "parts", []) or []):
            if hasattr(p, "text") and p.text:
                parts.append(p.text)
        message_str = " ".join(parts)

    match = re.search(r'(?i)(?::\s*)(yes|y|no|n)\s*$', message_str.strip())
    text_lower = match.group(1).lower() if match else message_str.strip().lower()

    if text_lower in ("yes", "y", "no", "n") and session and getattr(session, "events", None):
        pending_call_ids = []
        
        # Scan the last 15 events maximum backwards to find the most recent confirmation request
        for i in range(len(session.events)-1, max(-1, len(session.events)-15), -1):
            ev = session.events[i]
            
            # Check for actual 'adk_request_confirmation' tool calls in the event
            fcs = ev.get_function_calls() if hasattr(ev, "get_function_calls") else []
            for fc in fcs:
                if fc.name == "adk_request_confirmation" and fc.id:
                    pending_call_ids.append(fc.id)
            
            if pending_call_ids:
                break
        
        if pending_call_ids:
            is_confirmed = text_lower in ("yes", "y")
            func_parts = []
            for pc_id in pending_call_ids:
                fr = types.FunctionResponse(
                    id=pc_id, 
                    name="adk_request_confirmation", 
                    response={"hint": "", "confirmed": is_confirmed, "payload": None}
                )
                func_parts.append(types.Part(function_response=fr))
            # Critical: return a Content object with ONLY the FunctionResponse to unblock the agent
            message_arg = types.Content(role="user", parts=func_parts)
        else:
            # Not confirmed, or no pending call found
            message_arg = message if isinstance(message, types.Content) else types.Content(role="user", parts=[types.Part.from_text(text=message)])
    else:
        # Standard chat message
        message_arg = message if isinstance(message, types.Content) else types.Content(role="user", parts=[types.Part.from_text(text=message)])
    

    parts = []
    media_items = []
    # Running map of call_id -> tool_name accumulated across ALL events in the stream
    seen_function_calls = {}

    for attempt in range(1 + MAX_RETRIES):
        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=message_arg,
            ):
                # Track all function calls across the entire event stream
                if hasattr(event, "get_function_calls"):
                    for fc in event.get_function_calls():
                        if fc.id and fc.name:
                            seen_function_calls[fc.id] = fc.name

                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            parts.append(part.text)
                        # Capture inline binary data (images, audio, etc.)
                        elif hasattr(part, "inline_data") and part.inline_data:
                            media_items.append({
                                "data": part.inline_data.data,
                                "mime_type": part.inline_data.mime_type or "application/octet-stream",
                            })

                if getattr(event, "actions", None) and getattr(event.actions, "requested_tool_confirmations", None):
                    for call_id, confirmation in event.actions.requested_tool_confirmations.items():
                        tool_name = seen_function_calls.get(call_id, "an action")

                        # Include the hint from ToolConfirmation if available
                        hint_text = getattr(confirmation, "hint", "") or ""

                        msg = f"⚠️ **Action Requires Confirmation**\n\nThe agent wants to execute `{tool_name}`."
                        if hint_text:
                            msg += f"\n📋 Reason: {hint_text}"
                        msg += "\nPlease approve or deny by explicitly responding 'yes' or 'no'."

                        parts.append(msg)

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

            # Catch rate limit / quota errors gracefully
            if (
                "429" in error_msg
                or "RESOURCE_EXHAUSTED" in error_msg
                or "QuotaExceeded" in error_msg
            ):
                return AgentResponse(
                    text="⚠️ **Rate Limit Exceeded**\n\n"
                    "You've hit the API quota limit. Please wait a bit before trying again. "
                    "If this persists, check your billing details or rate limits."
                )

            # Catch token limit / context window errors
            if "token count exceeds" in error_msg.lower() or "400" in error_msg:
                logger.error(
                    "Context limit reached for session %s: %s", session_id, error_msg
                )
                return AgentResponse(
                    text="⚠️ **Context Limit Reached**\n\n"
                    "The conversation has become too large for me to process. "
                    "Please use the **/reset** command to start a fresh session or wait a few minutes."
                    f"Error: {error_msg}"
                )

            if attempt < MAX_RETRIES:
                parts.clear()
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

    final_text = (
        "\n".join(parts)
        if parts
        else "I processed your request but have no response to show."
    )

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

    if isinstance(message, str):
        content = types.Content(role="user", parts=[types.Part.from_text(text=message)])
    else:
        content = message

    event = Event(
        timestamp=time.time(),
        content=content
    )
    await runner.session_service.append_event(session, event)
