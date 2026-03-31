# Signal Based Architecture v4.0
import os
import sys
import threading
import logging
import inspect
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

EXIT_CODE_UPDATE = 100
EXIT_CODE_ROLLBACK = 101

def _schedule_restart(exit_code: int):
    threading.Timer(1.0, sys.exit, [exit_code]).start()

def update_self(tool_context: ToolContext) -> dict:
    """Pulls the latest code, rebuilds the Docker daemon, and restarts the agent."""
    _schedule_restart(EXIT_CODE_UPDATE)
    return {"status": "success", "message": "Updating system. The agent will be offline for a moment..."}

def trigger_rollback(tool_context: ToolContext) -> dict:
    """Reverts the git commit to the previous state and reboots the active container."""
    _schedule_restart(EXIT_CODE_ROLLBACK)
    return {"status": "success", "message": "Rolling back system. Reverting to previous stable state..."}

def session_refresh(mode: str, tool_context: ToolContext) -> dict:
    """Wipes or summarizes active user conversation histories to free context space."""
    return {"status": "success", "message": "Session refreshed."}

async def set_planner_mode(enabled: bool, tool_context: ToolContext) -> dict:
    """Dynamically enables/disables deep thought processing (BuiltInPlanner)."""
    return {"status": "success", "message": f"Thinker mode {'enabled' if enabled else 'disabled'}."}

async def execute_approved_action(token: str, tool_context: ToolContext) -> dict:
    """Executes a previously staged and now approved system action.

    This tool is called when the user provides an approval token (e.g., ACT-XXXXXX)
    for a sensitive operation like a system update or integration change.

    Args:
        token (str): The unique approval token provided by the guardrail.

    Returns:
        dict: The result of the executed tool.
    """
    from app.core.pending_actions import get_and_delete_action
    import app.tools as tools_module

    action = get_and_delete_action(token)
    if not action:
        logger.warning(f"Failed approval attempt with token: {token}")
        return {"status": "error", "message": "Invalid or expired approval token."}

    tool_name = action["tool_name"]
    args = action["args"]

    # Get the tool function from the central tools module
    tool_func = getattr(tools_module, tool_name, None)

    if not tool_func:
        logger.error(f"Approved tool '{tool_name}' not found in app.tools")
        return {"status": "error", "message": f"Critical Error: Approved tool '{tool_name}' is missing."}

    logger.info(f"Executing approved action: {tool_name} with args {args}")

    try:
        # Check if tool_func is async
        if inspect.iscoroutinefunction(tool_func):
            return await tool_func(**args, tool_context=tool_context)
        else:
            return tool_func(**args, tool_context=tool_context)
    except Exception as e:
        logger.exception(f"Error executing approved action {tool_name}")
        return {"status": "error", "message": f"Execution failed: {str(e)}"}
