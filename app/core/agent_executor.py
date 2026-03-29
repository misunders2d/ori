"""Platform-agnostic agent execution, session management, and context handling."""

import logging
import time
import uuid
import re
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

    # REFINED REGEX: Matches only if 'yes' or 'no' is the standalone word 
    # (optionally preceded by a colon for UI consistency).
    # Prevents matching 'n' in 'interaction'.
    clean_msg = message_str.strip().lower()
    match = re.fullmatch(r'(?i)(?:[:]\s*)?(yes|y|no|n)', clean_msg)
    text_lower = match.group(1).lower() if match else ""

    was_confirmation = False
    is_confirmed = False
    if text_lower in ("yes", "y", "no", "n") and session and getattr(session, "events", None):
        pending_call_ids = []
        
        # Scan history for the most recent confirmation request
        for i in range(len(session.events)-1, max(-1, len(session.events)-15), -1):
            ev = session.events[i]
            
            if hasattr(ev, "actions") and ev.actions and hasattr(ev.actions, "requested_tool_confirmations"):
                if ev.actions.requested_tool_confirmations:
                    for cid in ev.actions.requested_tool_confirmations.keys():
                        pending_call_ids.append(cid)
            
            if not pending_call_ids:
                fcs = ev.get_function_calls() if hasattr(ev, "get_function_calls") else []
                for fc in fcs:
                    if fc.name == "adk_request_confirmation" and fc.id:
                        pending_call_ids.append(fc.id)
            
            if pending_call_ids:
                break
        
        if pending_call_ids:
            logger.info("Intercepted user confirmation: %s for call IDs %s", text_lower, pending_call_ids)
            was_confirmation = True
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
            message_arg = message if isinstance(message, types.Content) else types.Content(role="user", parts=[types.Part.from_text(text=message)])
    else:
        message_arg = message if isinstance(message, types.Content) else types.Content(role="user", parts=[types.Part.from_text(text=message)])
    

    parts = []
    media_items = []
    # Key: call_id, Value: {name: str, args: dict}
    seen_function_calls = {}
    latest_tool_results = []

    for attempt in range(1 + MAX_RETRIES):
        try:
            async for event in runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=message_arg,
            ):
                if hasattr(event, "get_function_calls"):
                    for fc in event.get_function_calls():
                        if fc.id and fc.name:
                            # Store name and args from the function call for confirmation prompt build
                            seen_function_calls[fc.id] = {
                                "name": str(fc.name),
                                "args": getattr(fc, "args", {}) or {}
                            }

                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if hasattr(part, "text") and part.text:
                            parts.append(part.text)
                        elif hasattr(part, "function_response") and part.function_response:
                            res = part.function_response.response
                            if isinstance(res, dict) and "message" in res:
                                latest_tool_results.append(res["message"])
                            elif isinstance(res, dict) and "status" in res:
                                latest_tool_results.append(f"Status: {res['status']}")

                        elif hasattr(part, "inline_data") and part.inline_data:
                            media_items.append({
                                "data": part.inline_data.data,
                                "mime_type": part.inline_data.mime_type or "application/octet-stream",
                            })

                if getattr(event, "actions", None) and getattr(event.actions, "requested_tool_confirmations", None):
                    fr_names = {}
                    if event.content and event.content.parts:
                        for part in event.content.parts:
                            if hasattr(part, "function_response") and part.function_response:
                                fr = part.function_response
                                if fr.id and fr.name:
                                    fr_names[fr.id] = str(fr.name)

                    for call_id, confirmation in event.actions.requested_tool_confirmations.items():
                        # Retrieve original tool name and arguments from captured function call data
                        call_data = seen_function_calls.get(call_id, {})
                        tool_name = call_data.get("name") or fr_names.get(call_id) or "an action"
                        
                        # Args come from original FunctionCall (Boolean confirmation) 
                        # or confirmation.payload (Advanced confirmation)
                        fc_args = call_data.get("args") or {}
                        payload = getattr(confirmation, "payload", None)
                        
                        clean_payload = {}
                        if payload:
                            if hasattr(payload, "model_dump"):
                                clean_payload = payload.model_dump()
                            elif hasattr(payload, "dict"):
                                clean_payload = payload.dict()
                            elif isinstance(payload, dict):
                                clean_payload = payload
                            else:
                                try:
                                    clean_payload = {k: v for k, v in vars(payload).items() if not k.startswith("_")}
                                except Exception:
                                    pass
                        
                        # Merge args (fc_args usually has them for require_confirmation=True)
                        full_payload = {**fc_args, **clean_payload}
                        
                        agent_name = getattr(event, "author", "The agent") or "The agent"
                        
                        summary_parts = []
                        for k, v in full_payload.items():
                            if k == "tool_context": continue
                            val_str = str(v)
                            if len(val_str) > 100:
                                val_str = val_str[:97] + "..."
                            summary_parts.append(f"{k}: '{val_str}'")
                        
                        summary_text = ", ".join(summary_parts) if summary_parts else ""
                        hint_text = getattr(confirmation, "hint", "") or ""
                        is_generic_hint = "Please approve or reject" in hint_text or not hint_text

                        msg = f"⚠️ **Action Requires Confirmation**\n\n"
                        msg += f"**{agent_name}** wants to execute `{tool_name}`."
                        
                        reason = ""
                        if not is_generic_hint:
                            reason = hint_text
                        elif tool_name == "update_self":
                            reason = "Deploy latest code changes and restart the daemon."
                        elif tool_name == "trigger_rollback":
                            reason = "Revert to the previous stable git commit."
                        elif tool_name == "session_refresh":
                            mode = full_payload.get('mode', 'fresh')
                            reason = f"Clear conversation history (Mode: {mode})."
                        elif tool_name == "evolution_commit_and_push":
                            msg_arg = full_payload.get('commit_message', 'Perform code evolution')
                            summary_arg = full_payload.get('summary')
                            # Prioritize summary_arg if present
                            reason = f"Commit and push changes: {summary_arg if summary_arg else msg_arg}"
                        elif summary_text:
                            reason = summary_text
                            
                        if reason:
                            # Clean reason of any markdown-breaking characters
                            reason = reason.replace("*", "").replace("_", "").replace("`", "")
                            msg += f"\n📋 **Reason:** {reason}"
                        
                        msg += "\n\nPlease approve or deny by explicitly responding **'yes'** or **'no'**."
                        parts.append(msg)

            break  # success
        except Exception as exc:
            error_msg = str(exc).split("\n")[0] if str(exc) else type(exc).__name__
            logger.warning("Agent execution error: %s", error_msg)

            if attempt < MAX_RETRIES:
                parts.clear()
                message_arg = types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=f"Your previous action failed with this error: {error_msg}\nAnalyze what went wrong and retry.")]
                )
                continue
            return AgentResponse(text=f"Agent error after retries: {error_msg}")

    if not parts:
        if was_confirmation:
            if is_confirmed:
                if latest_tool_results:
                    final_text = f"✅ **Action Confirmed**\n\n{latest_tool_results[-1]}"
                else:
                    final_text = "✅ **Action Confirmed**\n\nThe operation was performed successfully."
            else:
                final_text = "❌ **Action Cancelled**\n\nI have cancelled the operation."
        else:
            final_text = "I processed your request but have no response to show."
    else:
        final_text = "\n".join(parts)

    refresh_mode = get_pending_refresh(session_id)
    if refresh_mode:
        refresh_msg = await _perform_session_refresh(runner, user_id, session_id, refresh_mode)
        final_text += f"\n\n--- SESSION REFRESHED ---\n{refresh_msg}"

    return AgentResponse(text=final_text, media_items=media_items)


async def process_message_for_context(runner, user_id: str, session_id: str, message: str | types.Content):
    """Silently add a message as context to the session."""
    try:
        from google.adk.events.event import Event
    except ImportError:
        import sys
        Event = sys.modules['google.adk.events.event'].Event

    session = await runner.session_service.get_session(app_name=runner.app_name, user_id=user_id, session_id=session_id)
    if session is None:
        session = await runner.session_service.create_session(app_name=runner.app_name, user_id=user_id, session_id=session_id)

    if isinstance(message, str):
        content = types.Content(role="user", parts=[types.Part.from_text(text=message)])
    else:
        content = message

    event = Event(id=str(uuid.uuid4()), author=user_id, timestamp=time.time(), content=content)
    await runner.session_service.append_event(session, event)
