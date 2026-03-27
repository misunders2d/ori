import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime

from google.adk.auth.auth_credential import AuthCredential, OAuth2Auth
from google.adk.auth.auth_schemes import OAuth2, OAuthGrantType
from google.adk.auth.auth_tool import AuthConfig
from google.adk.tools.tool_context import ToolContext


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
    notify = {}
    session = getattr(tool_context, "session", None)
    if session:
        sid = str(getattr(session, "id", ""))
        if sid.startswith("tg_chat_"):
            try:
                notify = {"type": "telegram", "chat_id": int(sid.replace("tg_chat_", ""))}
            except ValueError:
                pass
        elif sid.startswith("slack_channel_"):
            notify = {"type": "slack", "channel": sid.replace("slack_channel_", "")}

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

    sid = str(getattr(session, "id", ""))
    if not sid:
        return {"status": "error", "message": "Session ID not found."}

    from app.session_signals import request_refresh
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
    
    notify = {}
    session = getattr(tool_context, "session", None)
    if session:
        sid = str(getattr(session, "id", ""))
        if sid.startswith("tg_chat_"):
            try:
                notify = {"type": "telegram", "chat_id": int(sid.replace("tg_chat_", ""))}
            except ValueError:
                pass
        elif sid.startswith("slack_channel_"):
            notify = {"type": "slack", "channel": sid.replace("slack_channel_", "")}
            
    try:
        with open(trigger_path, "w") as f:
            _json.dump({"notify": notify}, f)
        return {
            "status": "success", 
            "message": "Rollback triggered. The system will revert and restart. I'll notify you when I'm back online."
        }
    except Exception as e:
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
