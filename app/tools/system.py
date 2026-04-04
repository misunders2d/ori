# Signal Based Architecture v5.0 (Clean Exit Edition)
#
# The agent NEVER calls os._exit(). Instead it writes a signal file and
# the transport layer (Telegram/Slack poller) checks for it after each
# message cycle and does a clean sys.exit(0). This eliminates the race
# condition where os._exit() could interrupt file writes (e.g. .env).
import os
import sys
import logging
import inspect
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

EXIT_CODE_UPDATE = 100
EXIT_CODE_ROLLBACK = 101
SIGNAL_FILE = os.path.abspath('./data/.exit_signal')


def _write_exit_signal(exit_code: int):
    """Write the exit signal file. The transport layer will read it and exit cleanly."""
    logger.info(f'CORE: Exit signal {exit_code} written. Waiting for clean shutdown...')
    try:
        with open(SIGNAL_FILE, 'w') as f:
            f.write(str(exit_code))
    except Exception as e:
        logger.error(f'Failed to write exit signal: {e}')


def check_exit_signal() -> bool:
    """Check if an exit signal is pending. Called by transport layers after each message cycle."""
    return os.path.exists(SIGNAL_FILE)


def execute_exit_signal():
    """Read the signal file and perform a clean exit. Called by transport layers."""
    if not os.path.exists(SIGNAL_FILE):
        return
    try:
        with open(SIGNAL_FILE, 'r') as f:
            code = f.read().strip()
        logger.info(f'CORE: Executing clean shutdown (signal: {code})...')
    except Exception:
        pass
    # Clean exit — sys.exit triggers finally blocks, flushes buffers, closes files
    sys.exit(0)


def _is_child_container() -> bool:
    """Detect if we're running as a spawned child (no .git, no launcher)."""
    return not os.path.isdir(os.path.join(os.path.dirname(__file__), '..', '..', '.git'))


def update_self(tool_context: ToolContext) -> dict:
    """Signals the system to restart. On the parent, the supervisor handles git pull + dep sync.
    On children, Docker's restart policy restarts the container (proving the process survives reboot)."""
    if _is_child_container():
        # Children reboot via direct exit — Docker restart: on-failure:3 brings them back.
        # No code changes on disk (children don't commit), just a clean restart to prove stability.
        # Safe to use threading.Timer here — children don't write to vault or .env.
        import threading
        logger.info('Child agent rebooting (clean exit for Docker restart)...')
        threading.Timer(2.0, lambda: sys.exit(0)).start()
        return {"status": "success", "message": "Child reboot initiated. Docker will restart the container."}
    logger.info('========================================')
    logger.info('🧬 [Ori System] PERIMETER LOCKDOWN: Exit signal dispatched (Code 100).')
    logger.info('========================================')
    _write_exit_signal(EXIT_CODE_UPDATE)
    return {"status": "success", "message": "Reboot signal dispatched. The system will shut down cleanly after this response is delivered."}

def trigger_rollback(tool_context: ToolContext) -> dict:
    """Signals the system to revert to the previous commit and rebuild."""
    _write_exit_signal(EXIT_CODE_ROLLBACK)
    return {"status": "success", "message": "Rollback signal dispatched. The system will shut down cleanly after this response is delivered."}

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
