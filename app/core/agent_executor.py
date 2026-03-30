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
            session = await runner.session_service.create_session(
                app_name=runner.app_name, user_id=user_id, session_id=session_id
            )

    except Exception:
        session = await runner.session_service.create_session(
            app_name=runner.app_name, user_id=user_id, session_id=session_id
        )

    MAX_RETRIES = 2

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

    # Extract the user's actual text from the enriched Telegram format:
    # "Message from Name (user_id): yes" → "yes"
    # Also handles bare messages like "yes" or ": yes"
    clean_msg = message_str.strip().lower()
    colon_pos = clean_msg.rfind(":")
    if colon_pos >= 0:
        val_to_check = clean_msg[colon_pos + 1:].strip()
    else:
        val_to_check = clean_msg

    # Accept natural affirmative/negative responses, not just "yes"/"no"
    _AFFIRM = {"yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "of course", "go ahead", "do it", "proceed", "confirm", "approved", "approve", "yes please", "do it", "okay, proceed"}
    _DENY = {"no", "n", "nah", "nope", "cancel", "stop", "deny", "denied", "reject", "abort"}
    is_confirmation_reply = (val_to_check in _AFFIRM or val_to_check in _DENY)

    was_confirmation = False
    is_confirmed = False

    if is_confirmation_reply and session and getattr(session, "events", None):
        pending_call_ids = []

        # Collect call_ids that have already been confirmed/denied via FunctionResponse
        already_responded = set()
        for ev in session.events:
            if ev.content and ev.content.parts:
                for part in (ev.content.parts or []):
                    if hasattr(part, "function_response") and part.function_response:
                        fr = part.function_response
                        if getattr(fr, "name", None) == "adk_request_confirmation" and fr.id:
                            already_responded.add(fr.id)

        # Map call IDs to their original tool names from history (Dynamic Name Recovery)
        call_id_to_name = {}
        for ev in reversed(session.events[-30:]):
            if ev.content and ev.content.parts:
                for part in ev.content.parts:
                    if hasattr(part, "function_call") and part.function_call:
                        fc = part.function_call
                        call_id_to_name[fc.id] = str(fc.name)

        # Scan history for the most recent UNRESOLVED confirmation request.
        # The ADK stores pending confirmations in event.actions.requested_tool_confirmations,
        # NOT as function calls — so we must check there.
        for i in range(len(session.events) - 1, max(-1, len(session.events) - 15), -1):
            ev = session.events[i]

            if getattr(ev, "actions", None) and getattr(ev.actions, "requested_tool_confirmations", None):
                for call_id in ev.actions.requested_tool_confirmations:
                    if call_id not in already_responded:
                        pending_call_ids.append(call_id)

            if pending_call_ids:
                break

        if pending_call_ids:
            was_confirmation = True
            is_confirmed = val_to_check in _AFFIRM
            logger.info("Confirmation reply identified: %s (confirmed=%s) for calls: %s", val_to_check, is_confirmed, pending_call_ids)
            func_parts = []
            for pc_id in pending_call_ids:
                # Use the real tool name if found, fallback to protocol name
                real_name = call_id_to_name.get(pc_id, "adk_request_confirmation")
                fr = types.FunctionResponse(
                    id=pc_id,
                    name=real_name,
                    response={"hint": "", "confirmed": is_confirmed, "payload": None}
                )
                func_parts.append(types.Part(function_response=fr))
            # Critical: return a Content object with ONLY the FunctionResponse to unblock the agent
            message_arg = types.Content(role="user", parts=func_parts)
        else:
            logger.info("Message was a confirmation keyword ('%s') but no pending calls were found in history.", val_to_check)
            # Not confirmed, or no pending call found
            message_arg = message if isinstance(message, types.Content) else types.Content(role="user", parts=[types.Part.from_text(text=message)])
    else:
        # Standard chat message
        message_arg = message if isinstance(message, types.Content) else types.Content(role="user", parts=[types.Part.from_text(text=message)])
    

    parts = []
    media_items = []
    # Running map of call_id -> tool_name accumulated across ALL events in the stream
    seen_function_calls = {}
    # Track latest tool results to provide better feedback if the model is silent
    latest_tool_results = []

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
                            seen_function_calls[fc.id] = str(fc.name)

                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            parts.append(part.text)
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

                if getattr(event, "actions", None) and getattr(event.actions, "requested_tool_confirmations", None):
                    # The ADK replaces FunctionCall with a FunctionResponse on confirmation events.
                    # Extract tool names from function_response parts on this event.
                    fr_names = {}
                    if event.content and event.content.parts:
                        for part in event.content.parts:
                            if hasattr(part, "function_response") and part.function_response:
                                fr = part.function_response
                                if fr.id and fr.name:
                                    fr_names[fr.id] = str(fr.name)

                    for call_id, confirmation in event.actions.requested_tool_confirmations.items():
                        # Primary: from the FunctionResponse on this event
                        # Fallback: from FunctionCalls seen earlier in the stream
                        tool_name = fr_names.get(call_id) or seen_function_calls.get(call_id) or "an action"
                        agent_name = getattr(event, "author", "The agent") or "The agent"

                        # Robust payload extraction
                        payload = getattr(confirmation, "payload", None)
                        summary_parts = []
                        clean_payload = {}

                        # Handle both dicts and objects for payload
                        if payload:
                            if isinstance(payload, dict):
                                items = payload.items()
                            else:
                                items = getattr(payload, "__dict__", {}).items()

                            for k, v in items:
                                if k != "tool_context" and not k.startswith("_"):
                                    clean_payload[k] = v
                                    val_str = str(v)
                                    if len(val_str) > 100:
                                        val_str = val_str[:97] + "..."
                                    summary_parts.append(f"{k}: '{val_str}'")

                        summary_text = ", ".join(summary_parts) if summary_parts else ""

                        # Use the hint from ToolConfirmation if available
                        hint_text = getattr(confirmation, "hint", "") or ""
                        # Check if the hint is the generic ADK one
                        is_generic_hint = "Please approve or reject" in hint_text or not hint_text

                        # Construct final message
                        msg = f"⚠️ **Action Requires Confirmation**\n\n"
                        msg += f"**{agent_name}** wants to execute `{tool_name}`."

                        # Generate a meaningful reason summary
                        reason = ""
                        if not is_generic_hint:
                            reason = hint_text
                        elif tool_name == "update_self":
                            reason = "Deploy latest code changes and restart the daemon."
                        elif tool_name == "trigger_rollback":
                            reason = "Revert to the previous stable git commit."
                        elif tool_name == "session_refresh":
                            mode = clean_payload.get('mode', 'fresh')
                            reason = f"Clear conversation history (mode: {mode})."
                        elif tool_name == "evolution_commit_and_push":
                            msg_arg = clean_payload.get('commit_message', 'code changes')
                            reason = f"Commit and push: {msg_arg}"
                        elif summary_text:
                            reason = summary_text

                        if reason:
                            msg += f"\n📋 **Reason:** {reason}"

                        msg += "\n\nPlease approve or deny by explicitly responding **'yes'** or **'no'**."
                        
                        logger.info("Presenting confirmation prompt to user for tool: %s", tool_name)
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
                    "Send /reset to start a fresh session."
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

    if not parts:
        if was_confirmation and is_confirmed:
            if latest_tool_results:
                final_text = f"✅ **Action Confirmed**\n\n{latest_tool_results[-1]}"
            else:
                final_text = "✅ **Action Confirmed**\n\nThe requested operation was performed successfully."
        elif was_confirmation and not is_confirmed:
            final_text = "❌ **Action Cancelled**\n\nThe requested operation was cancelled."
        else:
            final_text = "I processed your request but have no response to show."
    else:
        final_text = "\n".join(parts)

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
        id=str(uuid.uuid4()),
        author=user_id,
        timestamp=time.time(),
        content=content
    )
    await runner.session_service.append_event(session, event)
