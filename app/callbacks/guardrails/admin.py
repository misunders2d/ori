"""Admin-only gating: ACT-token staging + admin-only invocation gates.

`admin_tool_guardrail` is the workhorse — runs as `before_tool_callback`
and intercepts privileged tool calls (configure_integration, evolution_*,
get_my_a2a_key, trigger_rollback, reembed_entities, etc.) to stage an
ACT-token + TOTP confirmation flow. Two narrower sets bypass staging:
`update_self` and `inspect_secure_env` are admin-only but non-destructive
enough to skip the round-trip (one-prompt reboot UX, 2026-05-12 fix).

`admin_only_guardrail` is a `before_agent_callback`-style gate that
blocks the whole agent for non-admins. Currently orphan — not wired on
any agent. Kept for backward compat / potential future wiring.
"""

import logging
import os

from google.adk.agents.callback_context import CallbackContext
from google.genai import types

logger = logging.getLogger(__name__)


def admin_tool_guardrail(tool, args, tool_context, **kwargs) -> dict | None:
    """
    Runtime Guardrail: Intercepts highly privileged tool calls before execution.
    For Admin users, it stages the intent and requires a follow-up token approval.
    For non-Admin users, it blocks execution entirely.
    """
    if not tool or not tool_context:
        return None

    # Whitelist-based transfer guard: non-admins can only transfer to explicitly safe agents.
    # New sub-agents are blocked by default until added here.
    if tool.name == "transfer_to_agent":
        _NONADMIN_ALLOWED_AGENTS = {  # Agents with their own access control
            "ClickUpAgent",
            "BigQueryAgent",
            "AmazonHeadAgent",
            "AmazonAgent",
            "AmazonMemoryAgent",
            "AmazonWorkspaceAgent",
            "AmazonDataAnalystAgent",
        }
        agent_target = args.get("agent_name", "").strip()

        if agent_target.lower() not in {a.lower() for a in _NONADMIN_ALLOWED_AGENTS}:
            current_state = tool_context.state.to_dict()
            user_id = current_state.get("user_id", "")

            admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
            admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]

            is_a2a = user_id.startswith("A2A_USER_")
            if not is_a2a and (not admin_users or user_id not in admin_users):
                return {
                    "status": "error",
                    "message": f"Guardrail Intervention: Only Admin/Master users can transfer to `{agent_target}`. Your user_id ({user_id}) is unauthorized.",
                }
        return None

    # `reembed_entities` only stages when it would actually write. The
    # dry_run preview is cheap, read-only, and stating costs the admin
    # an extra TOTP round-trip for no real work.
    if tool.name == "reembed_entities" and args.get("dry_run", True):
        return None

    # Admin-only, no ACT-token staging required.
    # `update_self` is a clean process restart — supervisor brings the bot
    # back, no code or secret writes, fully reversible. ACT-token + TOTP
    # round-trip turned a one-prompt reboot into a 3-turn dance (provider
    # swap → reboot pain, 2026-05-12). Admin check remains; non-admins
    # are blocked below.
    # `inspect_secure_env` exposes which env vars are set (values redacted).
    # Useful for admin debugging; still revealing enough that non-admins
    # must be blocked.
    _ADMIN_ONLY_NO_STAGING = {"update_self", "inspect_secure_env"}
    if tool.name in _ADMIN_ONLY_NO_STAGING:
        current_state = tool_context.state.to_dict()
        user_id = current_state.get("user_id", "")
        admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
        admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]
        is_a2a = user_id.startswith("A2A_USER_")
        if not is_a2a and (not admin_users or user_id not in admin_users):
            return {
                "status": "error",
                "message": f"Guardrail Intervention: Only Admin/Master users can invoke `{tool.name}`. Your user_id ({user_id}) is unauthorized.",
            }
        return None

    if tool.name in [
        "configure_integration",
        "remove_integration",
        "schedule_system_task",
        "schedule_recurring_system_task",
        "run_system_task_now",
        "trigger_rollback",
        "evolution_commit_and_push",
        "evolution_git_pull",
        "evolution_git_reset",
        "evolution_sync_local_to_upstream",
        "get_my_a2a_key",
        "reembed_entities",
        # Telegram capability mutations (slice 10). Deterministic
        # `/cap grant|revoke` commands in the poller layer skip this
        # gate because they're already explicit admin keystrokes,
        # mirroring `/models set`. When the LLM calls these tools
        # programmatically, ACT+TOTP staging is required.
        "telegram_grant_capability",
        "telegram_revoke_capability",
    ]:
        current_state = tool_context.state.to_dict()
        user_id = current_state.get("user_id", "")
        session_id = current_state.get("session_id", "")
        if not session_id:
            session = getattr(tool_context, "session", None)
            session_id = (
                getattr(session, "session_id", None)
                or getattr(session, "id", None)
                or ""
            )

        logger.info(
            f"DEBUG: admin_tool_guardrail(tool={tool.name}) - user_id='{user_id}'"
        )

        admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
        admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]

        # A2A callers with validated API keys are trusted
        is_a2a = user_id.startswith("A2A_USER_")
        if not is_a2a and (not admin_users or user_id not in admin_users):
            return {
                "status": "error",
                "message": f"Guardrail Intervention: Only Admin/Master users can invoke `{tool.name}`. Your user_id ({user_id}) is unauthorized.",
            }

        # FOR ADMINS: Stage the intent if not already approved
        # This replaces the framework-level confirmation UI with a messenger-agnostic token protocol.
        try:
            from app.core.pending_actions import stage_action

            token = stage_action(tool.name, args, user_id, session_id)

            logger.info(f"Admin Guardrail: Staged {tool.name} for {user_id} -> {token}")

            totp_secret = os.environ.get("ADMIN_TOTP_SECRET")
            # 2FA Requirement: Required if ADMIN_TOTP_SECRET is set AND REQUIRE_2FA is true (default)
            require_2fa = os.environ.get("REQUIRE_2FA", "true").lower() == "true"

            if totp_secret and require_2fa:
                return {
                    "status": "error",  # Abort current execution
                    "message": (
                        f"**CRITICAL ACTION STAGED**\n\n"
                        f"To protect the system, the `{tool.name}` command requires explicit admin confirmation.\n\n"
                        f"Please reply with your token and 2FA code:\n"
                        f"`Approve {token} <your-6-digit-code>`\n\n"
                        f"_Note: This token expires in 15 minutes and is single-use._"
                    ),
                }
            else:
                return {
                    "status": "error",  # Abort current execution
                    "message": (
                        f"**CRITICAL ACTION STAGED**\n\n"
                        f"To protect the system, the `{tool.name}` command requires explicit admin confirmation.\n\n"
                        f"Please reply with:\n"
                        f"`Approve {token}`\n\n"
                        f"_Note: This token expires in 15 minutes and is single-use._"
                    ),
                }
        except Exception as e:
            logger.error(f"Failed to stage action in guardrail: {e}")
            return {
                "status": "error",
                "message": "Guardrail Error: Failed to stage your action for approval. Please check the logs.",
            }

    return None


def admin_only_guardrail(callback_context: CallbackContext) -> types.Content | None:
    """
    Runtime Guardrail: Checks if the user is explicitly set in ADMIN_USER_IDS setup.
    If not, it preemptively returns Content to halt execution of the agent.
    """
    current_state = callback_context.state.to_dict()
    user_id = current_state.get("user_id", "")

    admin_users_str = os.environ.get("ADMIN_USER_IDS", "")
    admin_users = [u.strip() for u in admin_users_str.split(",") if u.strip()]

    if not admin_users:
        return types.Content(
            parts=[
                types.Part(
                    text=f"Guardrail Intervention: ADMIN_USER_IDS is not configured in settings. Wait... Were you trying to find your ID to set this up? Here it is: `{user_id}`"
                )
            ]
        )

    # A2A callers with validated API keys are trusted (key was checked by a2a_server)
    if user_id.startswith("A2A_USER_"):
        return None  # Allow — authenticated A2A caller

    if user_id not in admin_users:
        return types.Content(
            parts=[
                types.Part(
                    text=f"Guardrail Intervention: Only Admin/Master users can invoke this agent. Your user_id (`{user_id}`) is unauthorized.\n\nTo add this ID to the admin list, run `update_self` from an already authorized platform and modify `ADMIN_USER_IDS`."
                )
            ]
        )

    return None
