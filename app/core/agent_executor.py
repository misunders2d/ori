"""Platform-agnostic agent execution, session management, and context handling."""

import logging
import time
import uuid

from google import genai
from google.genai import types

from app.session_signals import get_pending_refresh

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
) -> str:
    """Run the ADK runner and yield agent responses."""
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

    if isinstance(message, str):
        message_arg = types.Content(role="user", parts=[types.Part.from_text(text=message)])
        
        # Intercept tool confirmation "yes" / "no" and map to ADK FunctionResponse
        text_lower = message.strip().lower()
        if text_lower in ("yes", "y", "no", "n") and session and getattr(session, "events", None):
            pending_call_ids = []
            for i in range(len(session.events)-1, -1, -1):
                ev = session.events[i]
                if ev.author == 'user':
                    break  # Reached the last user message; stop looking
                
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
                message_arg = types.Content(role="user", parts=func_parts)

    else:
        message_arg = message

    parts = []
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
                            parts.append(part.text)

                if getattr(event, "actions", None) and getattr(event.actions, "requested_tool_confirmations", None):
                    for call_id, confirmation in event.actions.requested_tool_confirmations.items():
                        tool_name = "an action"
                        if hasattr(confirmation, "function_call") and hasattr(confirmation.function_call, "name"):
                            tool_name = confirmation.function_call.name
                        parts.append(
                            f"⚠️ **Action Requires Confirmation**\n\n"
                            f"The agent wants to execute `{tool_name}`.\n"
                            f"Please approve or deny by explicitly responding 'yes' or 'no'."
                        )

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
                return (
                    "⚠️ **Rate Limit Exceeded**\n\n"
                    "You've hit the API quota limit. Please wait a bit before trying again. "
                    "If this persists, check your billing details or rate limits."
                )

            # Catch token limit / context window errors
            if "token count exceeds" in error_msg.lower() or "400" in error_msg:
                logger.error(
                    "Context limit reached for session %s: %s", session_id, error_msg
                )
                return (
                    "⚠️ **Context Limit Reached**\n\n"
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
            return (
                f"Agent error after {1 + MAX_RETRIES} attempts. Last error: {error_msg}"
            )

    final_response = (
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
        final_response += f"\n\n--- SESSION REFRESHED ---\n{refresh_msg}"

    return final_response


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
