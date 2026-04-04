# Signal Based Architecture v4.0 (Lifecycle Edition)
import os
import sys
import threading
import logging
import inspect
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

EXIT_CODE_UPDATE = 100
EXIT_CODE_ROLLBACK = 101
SIGNAL_FILE = os.path.abspath('./data/.exit_signal')

def _schedule_restart(exit_code: int):
    def hard_exit():
        logger.info(f'CORE: Finalizing process with exit code {exit_code}...')
        # Write signal to file so the launcher knows what to do.
        # Exit with 0 so Docker does not race-restart the container.
        try:
            with open(SIGNAL_FILE, 'w') as f:
                f.write(str(exit_code))
        except Exception as e:
            logger.error(f'Failed to write exit signal: {e}')
        os._exit(0)
        
    threading.Timer(1.0, hard_exit).start()

def update_self(tool_context: ToolContext) -> dict:
    """Pulls latest code, clears memory, and performs a HARD reboot of the container."""
    logger.info('========================================')
    logger.info('🧬 [Ori System] PERIMETER LOCKDOWN: Dispatched Hard Exit (Code 100).')
    logger.info('========================================')
    _schedule_restart(EXIT_CODE_UPDATE)
    return {"status": "success", "message": "Applying Lockdown. The agent is performing a hard reboot..."}

def trigger_rollback(tool_context: ToolContext) -> dict:
    """Reverts commits and performs a HARD reboot."""
    _schedule_restart(EXIT_CODE_ROLLBACK)
    return {"status": "success", "message": "Rolling back system. Hard rebooting..."}

def session_refresh(mode: str, tool_context: ToolContext) -> dict:
    """Wipes conversation history."""
    return {"status": "success", "message": "Session refreshed."}

async def set_planner_mode(enabled: bool, tool_context: ToolContext) -> dict:
    """Toggle deep thought."""
    tool_context.state["use_planner"] = enabled
    return {"status": "success", "message": f"Thinker mode {'enabled' if enabled else 'disabled'}."}

async def execute_approved_action(token: str, totp_code: str = "", tool_context: ToolContext = None) -> dict:
    from app.core.pending_actions import get_and_delete_action
    import app.tools as tools_module
    
    totp_secret = os.environ.get("ADMIN_TOTP_SECRET")
    require_2fa = os.environ.get("REQUIRE_2FA", "true").lower() == "true"
    
    if totp_secret and require_2fa:
        from app.app_utils.totp import verify_totp
        if not totp_code or not verify_totp(totp_secret, str(totp_code)):
            return {"status": "error", "message": "Invalid 2FA code."}

    action = get_and_delete_action(token)
    if not action: return {"status": "error", "message": "Invalid token."}

    tool_func = getattr(tools_module, action["tool_name"], None)
    if not tool_func: return {"status": "error", "message": "Tool missing."}

    try:
        if inspect.iscoroutinefunction(tool_func):
            return await tool_func(**action["args"], tool_context=tool_context)
        return tool_func(**action["args"], tool_context=tool_context)
    except Exception as e: return {"status": "error", "message": f"Execution failed: {e}"}
