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
    logger.info("========================================")
    logger.info("🧬 [Ori System] UPDATE STARTED: Dispatched Exit Code 100.")
    logger.info("========================================")
    _schedule_restart(EXIT_CODE_UPDATE)
    return {"status": "success", "message": "Updating system. The agent will be offline for a moment..."}

def trigger_rollback(tool_context: ToolContext) -> dict:
    """Reverts the git commit to the previous state and reboots the active container."""
    logger.info("========================================")
    logger.info("🧬 [Ori System] ROLLBACK STARTED: Dispatched Exit Code 101.")
    logger.info("========================================")
    _schedule_restart(EXIT_CODE_ROLLBACK)
    return {"status": "success", "message": "Rolling back system. Reverting to previous stable state..."}

def session_refresh(mode: str, tool_context: ToolContext) -> dict:
    """Wipes or summarizes active user conversation histories to free context space."""
    return {"status": "success", "message": "Session refreshed."}

async def set_planner_mode(enabled: bool, tool_context: ToolContext) -> dict:
    """Dynamically enables/disables deep thought processing (BuiltInPlanner)."""
    return {"status": "success", "message": f"Thinker mode {'enabled' if enabled else 'disabled'}."}

def check_active_tasks(tool_context: ToolContext) -> dict:
    """Checks the real-time status of all currently executing background and system tasks.
    
    Use this tool when the user asks 'what's running?', 'is the task done?', or 'status of the background job'.
    
    Returns:
        dict: A list of active tasks with their start time, current status, and prompt.
    """
    try:
        from app.tasks import ACTIVE_TASKS
        if not ACTIVE_TASKS:
            return {"status": "success", "message": "There are no background tasks currently being tracked in active memory."}
            
        task_list = []
        for tid, data in ACTIVE_TASKS.items():
            task_list.append({
                "task_id": tid,
                "type": data.get("type", "unknown"),
                "status": data.get("status", "Unknown"),
                "start_time": data.get("start_time", "N/A"),
                "end_time": data.get("end_time"),
                "prompt": data.get("prompt", "")
            })
            
        return {"status": "success", "active_tasks": task_list}
    except Exception as e:
        logger.error(f"Failed to check active tasks: {e}")
        return {"status": "error", "message": f"Failed to check active tasks: {e}"}

def repair_data_permissions(tool_context: ToolContext = None) -> dict:
    """Attempts to fix 'readonly database' errors by correcting file permissions in the data directory.
    
    This tool recursively sets the data directory to be writable by the current process.
    """
    data_dir = os.path.abspath("./data")
    if not os.path.exists(data_dir):
        return {"status": "error", "message": f"Data directory not found at {data_dir}"}

    try:
        count = 0
        # 1. Fix directory permissions (777)
        os.chmod(data_dir, 0o777)
        
        # 2. Fix file permissions (666)
        for root, dirs, files in os.walk(data_dir):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o777)
            for f in files:
                os.chmod(os.path.join(root, f), 0o666)
                count += 1
        
        logger.info(f"FileSystem Repair: Corrected permissions for {count} files in {data_dir}")
        return {"status": "success", "message": f"Successfully repaired permissions for {count} items in the data directory."}
    except Exception as e:
        logger.error(f"FileSystem Repair Failed: {e}")
        return {"status": "error", "message": f"Failed to repair permissions: {e}"}

async def execute_approved_action(token: str, totp_code: str = "", tool_context: ToolContext = None) -> dict:
    """Executes a previously staged and now approved system action.

    This tool is called when the user provides an approval token (e.g., ACT-XXXXXX)
    for a sensitive operation like a system update or integration change.

    Args:
        token (str): The unique approval token provided by the guardrail.
        totp_code (str): The 6-digit TOTP authenticator code (required if 2FA is enabled).

    Returns:
        dict: The result of the executed tool.
    """
    from app.core.pending_actions import get_and_delete_action
    import app.tools as tools_module
    import os
    
    totp_secret = os.environ.get("ADMIN_TOTP_SECRET")
    if totp_secret:
        from app.app_utils.totp import verify_totp
        if not totp_code or not verify_totp(totp_secret, str(totp_code)):
            return {"status": "error", "message": "Invalid or missing TOTP 2FA code. Please request the action again and provide a valid code."}

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
