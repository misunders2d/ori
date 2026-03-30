import os
import shutil
import subprocess
import sys
import uuid
import logging
from datetime import datetime

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


def update_self(tool_context: ToolContext) -> dict:
    """Triggers a self-update: pulls the latest code from git and rebuilds the Docker container.

    Use this when the user asks to update the bot, deploy latest changes, or pull new code.
    The bot will go offline briefly during the rebuild and come back automatically.
    The user will be notified when the update is complete.

    Returns:
        dict: Status of the update trigger.
    """

    import json as _json

    trigger_path = os.path.abspath("./data/.update_trigger")

    # Determine who to notify after rebuild
    from app.core.transport import parse_notify_from_session_id

    notify = {}
    session = getattr(tool_context, "session", None)
    if session:
        sid = getattr(session, "session_id", None) or getattr(session, "id", None)
        notify = parse_notify_from_session_id(sid)

    logger.info("TRIGGER: update_self called. Notify info: %s", notify)

    try:
        with open(trigger_path, "w") as f:
            _json.dump({
                "requested_at": datetime.now().isoformat(),
                "notify": notify,
            }, f)
        return {
            "status": "success",
            "message": "Update triggered. The bot will pull the latest code, rebuild, and restart. "
                       "I'll notify you when the update is complete.",
        }
    except Exception as e:
        logger.error("TRIGGER: update_self failed: %s", e)
        return {"status": "error", "message": f"Failed to trigger update: {e}"}



def session_refresh(mode: str, tool_context: ToolContext) -> dict:
    """Triggers a session refresh (clearing conversation history).

    Use this when the user explicitly asks to "refresh session", "start over",
    or "clear history".

    Args:
        mode (str): The refresh mode. Must be one of:
            - 'fresh' — Wipe the session completely.
            - 'summarize' — Summarize the current session and carry it over to a new one.

    Returns:
        dict: Status of the refresh request.
    """
    mode = mode.lower().strip()
    if mode not in ["fresh", "summarize"]:
        return {"status": "error", "message": "Invalid mode. Use 'fresh' or 'summarize'."}

    session = getattr(tool_context, "session", None)
    if not session:
        return {"status": "error", "message": "No active session found."}

    sid = getattr(session, "session_id", None) or getattr(session, "id", None)
    if not sid:
        return {"status": "error", "message": "Session ID not found."}

    from app.session_signals import request_refresh
    logger.info("TRIGGER: session_refresh called with mode: %s for session: %s", mode, sid)
    request_refresh(sid, mode)

    return {
        "status": "success",
        "message": f"Session refresh ({mode}) triggered. This will take effect after my next response.",
    }


def trigger_rollback(tool_context: ToolContext) -> dict:
    """Triggers a system rollback to the previous git commit and restarts the daemon.
    
    Use this when the user asks to revert the codebase, rollback a bug, or undo a recent feature.
    
    Returns:
        dict: Status of the rollback trigger.
    """

    import json as _json
    trigger_path = os.path.abspath("./data/.rollback_trigger")
    
    from app.core.transport import parse_notify_from_session_id

    notify = {}
    session = getattr(tool_context, "session", None)
    if session:
        sid = getattr(session, "session_id", None) or getattr(session, "id", None)
        notify = parse_notify_from_session_id(sid)
            
    logger.info("TRIGGER: trigger_rollback called. Notify info: %s", notify)

    try:
        with open(trigger_path, "w") as f:
            _json.dump({"notify": notify}, f)
        return {
            "status": "success", 
            "message": "Rollback triggered. The system will revert and restart. I'll notify you when I'm back online."
        }
    except Exception as e:
        logger.error("TRIGGER: trigger_rollback failed: %s", e)
        return {"status": "error", "message": f"Failed to trigger rollback: {e}"}


async def set_planner_mode(enabled: bool, tool_context: ToolContext) -> dict:
    """Enables or disables the deep execution planner mode (Thinker mode).
    
    Use this when the user asks to turn on thinking, enable the planner, or turn off thinking.
    
    Args:
        enabled (bool): True to enable thinking, False to disable.
    """

    session = getattr(tool_context, "session", None)
    if not session:
        return {"status": "error", "message": "No active session found."}
        
    uid = getattr(session, "user_id", "")
    
    from run_bot import get_runner
    runner = get_runner()
    if not runner:
        return {"status": "error", "message": "Runner not available."}
        
    import uuid
    from google.adk.events.event import Event, EventActions
    
    await runner.session_service.append_event(
        session=session,
        event=Event(
            id=str(uuid.uuid4()),
            author="__system__",
            content=None,
            actions=EventActions(state_delta={"use_planner": enabled}),
        ),
    )
    return {
        "status": "success",
        "message": f"Planner mode set to {enabled}."
    }

async def execute_approved_action(token: str, tool_context: ToolContext) -> dict:
    """Executes a previously staged and approved system action using its unique Token ID.

    Use this when the user provides an approval token (e.g., 'ACT-8A4F9X') for a
    sensitive operation like a system update or integration change.

    Args:
        token (str): The Action Token ID to execute.

    Returns:
        dict: Result of the action execution.
    """
    from app.core.pending_actions import get_and_delete_action
    
    action = get_and_delete_action(token)
    if not action:
        return {
            "status": "error", 
            "message": f"Action ID '{token}' not found, already used, or expired (15-min TTL). "
                       f"Please try the original command again to generate a new token."
        }

    # Security Gate: Use the state-persistent user_id (individual ID) instead of the session runner ID (chat ID)
    # This ensures consistency with how the guardrail staged the action.
    current_state = tool_context.state.to_dict()
    current_user_id = current_state.get("user_id", "")
    
    logger.info(f"DEBUG: execute_approved_action(token={token}) - action['user_id']='{action['user_id']}', current_user_id='{current_user_id}'")

    if not current_user_id:
        # Fallback to session.user_id if state is missing
        session = getattr(tool_context, "session", None)
        current_user_id = getattr(session, "user_id", "") if session else ""
        logger.info(f"DEBUG: Fallback current_user_id='{current_user_id}'")

    if current_user_id != action["user_id"]:
         logger.warning(f"Security Violation: Token {token} staged by {action['user_id']} but execution attempted by {current_user_id}")
         return {
             "status": "error", 
             "message": "Security Violation: This action token was generated for a different user and cannot be executed by you."
         }

    # Double-check ADMIN_USER_IDS in case environment changed
    admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
    admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]
    if current_user_id not in admin_users:
        return {"status": "error", "message": f"Unauthorized user: {current_user_id}"}

    # Tunnel Execution: Call the original tool function directly, bypassing the tool-level guardrail
    tool_name = action["tool_name"]
    args = action["args"]
    
    logger.info(f"Executing approved action: {tool_name} with args {args}")

    try:
        if tool_name == "update_self":
            return update_self(tool_context)
        elif tool_name == "session_refresh":
            return session_refresh(mode=args.get("mode", "fresh"), tool_context=tool_context)
        elif tool_name == "trigger_rollback":
            return trigger_rollback(tool_context)
        elif tool_name == "set_planner_mode":
            # Correctly await the async function instead of trying to run a second event loop
            return await set_planner_mode(enabled=args.get("enabled", False), tool_context=tool_context)
        elif tool_name == "configure_integration":
            from app.tools.integrations import configure_integration
            return configure_integration(
                integration_name=args.get("integration_name"),
                config_json=args.get("config_json"),
                tool_context=tool_context
            )
        elif tool_name == "remove_integration":
            from app.tools.integrations import remove_integration
            return remove_integration(
                integration_name=args.get("integration_name"),
                tool_context=tool_context
            )
        elif tool_name == "run_system_task_now":
            from app.tools.scheduling import run_system_task_now
            return run_system_task_now(task_id=args.get("task_id"), tool_context=tool_context)
        elif tool_name == "schedule_system_task":
            from app.tools.scheduling import schedule_system_task
            return schedule_system_task(
                task_id=args.get("task_id"),
                interval_minutes=args.get("interval_minutes"),
                args=args.get("args"),
                tool_context=tool_context
            )
        elif tool_name == "schedule_recurring_system_task":
             from app.tools.scheduling import schedule_recurring_system_task
             return schedule_recurring_system_task(
                 task_id=args.get("task_id"),
                 cron=args.get("cron"),
                 args=args.get("args"),
                 tool_context=tool_context
             )
        else:
            return {"status": "error", "message": f"Unsupported staged tool: {tool_name}"}
    except Exception as e:
        logger.error(f"Failed to execute approved action {tool_name}: {e}")
        return {"status": "error", "message": f"Execution failed: {e}"}
