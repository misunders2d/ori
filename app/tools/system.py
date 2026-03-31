# Signal Based Architecture v4.0
import os
import sys
import threading
import logging
import asyncio
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

EXIT_CODE_UPDATE = 100
EXIT_CODE_ROLLBACK = 101

def _schedule_restart(exit_code: int):
    threading.Timer(1.0, sys.exit, [exit_code]).start()

def update_self(tool_context: ToolContext) -> dict:
    _schedule_restart(EXIT_CODE_UPDATE)
    return {"status": "success", "message": "Update signal dispatched. The daemon is restarting to pull new DNA..."}

def trigger_rollback(tool_context: ToolContext) -> dict:
    _schedule_restart(EXIT_CODE_ROLLBACK)
    return {"status": "success", "message": "Rollback signal dispatched. Reverting to previous DNA..."}

def session_refresh(mode: str, tool_context: ToolContext) -> dict:
    return {"status": "success", "message": f"Session refreshed with mode: {mode}."}

async def set_planner_mode(enabled: bool, tool_context: ToolContext) -> dict:
    return {"status": "success", "message": f"Planner (Thinker) mode set to {enabled}."}

async def execute_approved_action(token: str, tool_context: ToolContext) -> dict:
    """
    Executes a highly privileged system action that was previously staged and has now been approved.
    
    Args:
        token (str): The unique approval token (e.g., 'ACT-8A4F9X').
    """
    from app.core.pending_actions import get_and_delete_action
    
    # 1. Fetch and validate the token
    action = get_and_delete_action(token)
    if not action:
        return {
            "status": "error", 
            "message": f"Invalid or expired token: {token}. You may need to request the action again."
        }
        
    tool_name = action["tool_name"]
    args = action["args"]
    
    logger.info(f"Executing approved action: {tool_name} with args: {args}")

    # 2. Dynamically locate the target tool
    # Check this module first (system tasks)
    target_func = globals().get(tool_name)
    
    if not target_func:
        # Check integrations module (for configure_integration, etc.)
        import app.tools.integrations as integrations_module
        target_func = getattr(integrations_module, tool_name, None)
        
    if not target_func:
        # Check scheduling module (for schedule_system_task, etc.)
        import app.tools.scheduling as scheduling_module
        target_func = getattr(scheduling_module, tool_name, None)

    if not target_func:
        return {
            "status": "error",
            "message": f"Critical Error: Approved tool '{tool_name}' could not be located in the registry."
        }
        
    # 3. Execute the tool
    try:
        if asyncio.iscoroutinefunction(target_func):
            result = await target_func(tool_context=tool_context, **args)
        else:
            result = target_func(tool_context=tool_context, **args)
            
        return {
            "status": "success",
            "message": f"Action `{tool_name}` successfully executed.",
            "tool_result": result
        }
    except Exception as e:
        logger.exception(f"Failed to execute approved action {tool_name}")
        return {
            "status": "error",
            "message": f"Action `{tool_name}` crashed during execution: {str(e)}"
        }
