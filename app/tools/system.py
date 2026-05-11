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


def consume_exit_signal() -> bool:
    """Check and log exit signal. Returns True if shutdown should proceed.

    Called by transport layers after each message cycle. The transport
    should `return` from its coroutine (not sys.exit) to let asyncio
    shut down cleanly without traceback cascades.
    """
    if not os.path.exists(SIGNAL_FILE):
        return False
    try:
        with open(SIGNAL_FILE, 'r') as f:
            code = f.read().strip()
        logger.info(f'CORE: Clean shutdown requested (signal: {code})...')
    except Exception:
        pass
    return True


def _is_child_container() -> bool:
    """Detect if we're running as a spawned child (no .git, no launcher).

    Worktree-safe: `.git` may be a regular file (gitdir pointer), not a
    directory. `os.path.exists` covers both shapes. See May 2026
    rescue retrospective.
    """
    return not os.path.exists(os.path.join(os.path.dirname(__file__), '..', '..', '.git'))


def update_self(tool_context: ToolContext) -> dict:
    """Real process restart/reboot via supervisor exit signal.

    Use only when the user clearly wants the running agent process/app to
    restart so code, dependency, or cross-provider model changes take effect.
    Do NOT use for conversation reset/history wipe; that is session_refresh.
    If the user only says "reset", "restart", "refresh", or "reboot" without
    naming the target, ask whether they mean process restart or session refresh
    before calling any lifecycle tool.
    """
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
    """Rollback code to previous commit and rebuild/restart.

    Use only when the user clearly asks to roll back code or revert the last
    deployed commit. Do NOT use for session refresh or normal process restart.
    If "reset" is ambiguous, ask which reset target the user means first.
    """
    _write_exit_signal(EXIT_CODE_ROLLBACK)
    # Lazy import to avoid circular dependency at module load.
    try:
        from app.tools.evolution import _audit_actor, _evolution_audit
        _evolution_audit("rollback", _audit_actor(tool_context), "ok")
    except Exception:
        pass
    return {"status": "success", "message": "Rollback signal dispatched. The system will shut down cleanly after this response is delivered."}

def session_refresh(mode: str, tool_context: ToolContext) -> dict:
    """Conversation/session reset only; wipes or summarizes chat history.

    This does NOT restart the running process and does NOT reload model objects.
    Do NOT use when the user wants model assignments, code changes, or provider
    changes to take effect; that requires update_self. If the user only says
    "reset", "restart", "refresh", or "reboot" without naming the target, ask
    whether they mean session refresh or process restart before calling any
    lifecycle tool.

    Args:
        mode: 'fresh' for a clean wipe, 'summarize' to condense history first.
    """
    from app.session_signals import request_refresh

    session = getattr(tool_context, "session", None)
    if not session:
        return {"status": "error", "message": "No active session to refresh."}

    session_id = getattr(session, "session_id", None) or getattr(session, "id", None)
    if not session_id:
        return {"status": "error", "message": "Could not determine session ID."}

    if mode not in ("fresh", "summarize"):
        mode = "fresh"

    request_refresh(session_id, mode)
    return {"status": "success", "message": f"Session refresh ({mode}) scheduled. It will take effect after this response."}

async def set_thinking_mode(
    enabled: bool,
    tool_context: ToolContext,
    budget_tokens: int = 4096,
) -> dict:
    """Toggle extended thinking globally across every sub-agent.

    Provider-agnostic: applies to Gemini (per-turn ``thinking_config`` set
    in the ``state_setter`` callback) and to LiteLlm-backed agents like
    Anthropic Opus 4.7 via OpenRouter (``thinking={"type":"enabled",...}``
    injected into each LiteLlm instance's completion kwargs).

    Persisted to ``data/thinking_config.json`` so the setting survives
    restart. The flag is process-global — one call, one effect, all
    agents — there is no per-session override.

    Thoughts themselves never reach Slack/Telegram/A2A. ``extract_agent_response``
    filters ``Part(thought=True)`` regardless of this flag — turning
    thinking on lets the model reason internally without polluting chat.

    Args:
        enabled: True to allow models to think before answering. False to
            forbid it.
        budget_tokens: How many thinking tokens Anthropic is allowed per
            response when enabled. Ignored when disabled. Default 4096.

    Returns:
        dict with the new persisted config + a count of agents updated.
    """
    from app.app_utils import thinking

    cfg = thinking.save(enabled, budget_tokens=budget_tokens)

    # Apply to the live agent tree so the next turn already uses the new
    # setting. Without this, LiteLlm-backed agents would only pick up the
    # change on the next process restart.
    counts = {"inspected": 0, "mutated": 0}
    try:
        from app.agent import root_agent

        counts = thinking.apply_to_agent_tree(root_agent)
    except Exception as e:
        logger.warning("apply_to_agent_tree failed in set_thinking_mode: %s", e)

    return {
        "status": "success",
        "message": (
            f"Thinking {'enabled' if enabled else 'disabled'} globally "
            f"(budget={budget_tokens} tokens). "
            f"Applied to {counts['mutated']}/{counts['inspected']} LiteLlm agents; "
            "Gemini agents pick up per-turn."
        ),
        "config": cfg,
    }


# Backward-compat alias: older sessions/skills may still call
# ``set_planner_mode``. Delegates to the new global toggle so behaviour
# converges on a single source of truth.
async def set_planner_mode(enabled: bool, tool_context: ToolContext) -> dict:
    """Deprecated alias for ``set_thinking_mode``. Use that instead."""
    return await set_thinking_mode(enabled, tool_context)

async def execute_approved_action(token: str, totp_code: str = "", tool_context: ToolContext = None) -> dict:
    from app.core.pending_actions import get_and_delete_action
    import app.tools as tools_module

    current_user_id = ""
    current_session_id = ""
    if tool_context is not None:
        state = getattr(tool_context, "state", None)
        if state is not None:
            try:
                state_dict = state.to_dict()
            except Exception:
                state_dict = {}
            current_user_id = state_dict.get("user_id", "")
            current_session_id = state_dict.get("session_id", "")
        if not current_session_id:
            session = getattr(tool_context, "session", None)
            current_session_id = (
                getattr(session, "session_id", None)
                or getattr(session, "id", None)
                or ""
            )

    totp_secret = os.environ.get("ADMIN_TOTP_SECRET")
    require_2fa = os.environ.get("REQUIRE_2FA", "true").lower() == "true"

    if totp_secret and require_2fa:
        from app.app_utils.totp import verify_totp
        if not totp_code or not verify_totp(totp_secret, str(totp_code)):
            return {"status": "error", "message": "Invalid 2FA code."}

    action = get_and_delete_action(token)
    if not action: return {"status": "error", "message": "Invalid token."}

    action_user_id = action.get("user_id", "")
    action_session_id = action.get("session_id", "")
    if action_user_id and current_user_id and action_user_id != current_user_id:
        return {"status": "error", "message": "Approval token does not belong to this user."}
    if action_session_id and current_session_id and action_session_id != current_session_id:
        return {"status": "error", "message": "Approval token does not belong to this session."}

    tool_func = getattr(tools_module, action["tool_name"], None)
    if not tool_func: return {"status": "error", "message": "Tool missing."}

    try:
        if inspect.iscoroutinefunction(tool_func):
            return await tool_func(**action["args"], tool_context=tool_context)
        return tool_func(**action["args"], tool_context=tool_context)
    except Exception as e: return {"status": "error", "message": f"Execution failed: {e}"}
