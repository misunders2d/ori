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

async def set_thinking_level(
    component: str,
    level: str,
    tool_context: ToolContext,
) -> dict:
    """Set the thinking level for one Ori component.

    Gemini 3 supports four levels: `minimal` (no thinking), `low`
    (minimum latency + cost), `medium` (balanced — codegen / SQL synth),
    `high` (maximum reasoning depth). Anthropic-backed agents map the
    same levels to budget_tokens (minimal/low → off; medium → 4096;
    high → 8192).

    Persisted to `data/thinking_config.json` (survives restart). Applied
    to the live agent tree immediately — Gemini agents pick up per-turn,
    LiteLlm-backed agents (Anthropic via OpenRouter) re-armed via
    `apply_to_agent_tree`.

    Defaults per component (see `app/app_utils/thinking.py:THINKING_DEFAULTS`):
    Coordinator / AmazonHead / Knowledge = `low`. CRUD leaves
    (AmazonAgent, AmazonMemory, AmazonWorkspace, ClickUp, google_search)
    = `minimal`. Code/SQL synth (DataAnalyst, BigQuery, DeveloperAgent)
    = `medium`. Summarizers = `minimal`.

    Args:
        component: Component name (e.g. 'CoordinatorAgent', 'AmazonAgent',
            'DeveloperAgent'). Must be in `THINKING_DEFAULTS`.
        level: One of 'minimal', 'low', 'medium', 'high'.

    Returns:
        dict with the new effective level for the component + how many
        live agents were updated.
    """
    from app.app_utils import thinking

    try:
        overrides = thinking.save_level(component, level)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    # Apply to live tree so the next turn picks up the new level.
    counts = {"inspected": 0, "mutated": 0}
    try:
        from app.agent import root_agent
        counts = thinking.apply_to_agent_tree(root_agent)
    except Exception as e:
        logger.warning("apply_to_agent_tree failed in set_thinking_level: %s", e)

    return {
        "status": "success",
        "message": (
            f"Thinking level for {component} set to '{level}'. "
            f"Re-armed {counts['mutated']}/{counts['inspected']} LiteLlm agents; "
            "Gemini agents pick up per-turn."
        ),
        "component": component,
        "level": level,
        "overrides": overrides,
    }


async def reset_thinking_level(
    component: str,
    tool_context: ToolContext,
) -> dict:
    """Clear a component's thinking override, reverting to its default.

    Args:
        component: Component name.
    Returns:
        dict with status + whether anything was cleared.
    """
    from app.app_utils import thinking

    try:
        cleared = thinking.reset_level(component)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    default_level = thinking.THINKING_DEFAULTS.get(component, "low")

    # Re-arm live tree if Anthropic-backed (Gemini picks up per-turn).
    try:
        from app.agent import root_agent
        thinking.apply_to_agent_tree(root_agent)
    except Exception as e:
        logger.warning("apply_to_agent_tree failed in reset_thinking_level: %s", e)

    if cleared:
        return {
            "status": "success",
            "message": f"Override cleared. {component} reverted to default level '{default_level}'.",
            "component": component,
            "level": default_level,
        }
    return {
        "status": "success",
        "message": f"No override set for {component}. Default '{default_level}' already in effect.",
        "component": component,
        "level": default_level,
    }


def list_thinking_levels(tool_context: ToolContext) -> dict:
    """Return the current effective thinking level for every component."""
    from app.app_utils import thinking
    levels = thinking.load_all_levels()
    return {"status": "success", "levels": levels}


# Backward-compat: older sessions/skills may still call set_thinking_mode.
# Delegates to the new per-component API by applying a blanket policy:
# enabled=True → all components on `medium` (or `high` if budget >= 8192);
# enabled=False → all components on `low`.
async def set_thinking_mode(
    enabled: bool,
    tool_context: ToolContext,
    budget_tokens: int = 4096,
) -> dict:
    """Deprecated. Use `set_thinking_level(component, level)` instead.

    Kept as a compatibility shim that applies a blanket policy across
    every component. The fine-grained tool is strongly preferred.
    """
    from app.app_utils import thinking

    blanket = "low"
    if enabled:
        blanket = "high" if budget_tokens >= 8192 else "medium"
    for component in thinking.THINKING_DEFAULTS:
        try:
            thinking.save_level(component, blanket)
        except ValueError:
            pass

    counts = {"inspected": 0, "mutated": 0}
    try:
        from app.agent import root_agent
        counts = thinking.apply_to_agent_tree(root_agent)
    except Exception as e:
        logger.warning("apply_to_agent_tree failed in set_thinking_mode: %s", e)

    return {
        "status": "success",
        "message": (
            f"DEPRECATED: blanket policy applied — every component set to '{blanket}'. "
            f"Use `set_thinking_level(component, level)` for per-agent control. "
            f"Re-armed {counts['mutated']}/{counts['inspected']} LiteLlm agents."
        ),
        "config": thinking.load(),
    }


async def set_planner_mode(enabled: bool, tool_context: ToolContext) -> dict:
    """Deprecated alias for `set_thinking_mode`. Prefer `set_thinking_level`."""
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
