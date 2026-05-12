import os

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _is_child_container() -> bool:
    """Detect if we're running as a spawned child (no .git, no Docker).

    Worktree-safe: `.git` may be a regular file (gitdir pointer), not a
    directory. `os.path.exists` covers both shapes. See May 2026
    rescue retrospective.
    """
    return not os.path.exists(os.path.join(_PROJECT_ROOT, ".git"))


class SystemToolset(BaseToolset):
    """Core system lifecycle tools."""

    async def get_tools(self, readonly_context=None):
        from app.tools.system import (
            update_self,
            session_refresh,
            trigger_rollback,
            set_thinking_mode,
            set_thinking_level,
            reset_thinking_level,
            list_thinking_levels,
            execute_approved_action,
        )
        from app.tools.diagnostics import (
            check_active_tasks,
            report_health,
            inspect_secure_env,
        )

        tools = [
            FunctionTool(func=update_self),
            FunctionTool(func=session_refresh),
            FunctionTool(func=set_thinking_level),
            FunctionTool(func=reset_thinking_level),
            FunctionTool(func=list_thinking_levels),
            FunctionTool(func=set_thinking_mode),  # deprecated, kept for back-compat
            FunctionTool(func=execute_approved_action),
            FunctionTool(func=check_active_tasks),
            FunctionTool(func=report_health),
            FunctionTool(func=inspect_secure_env),
        ]

        if not _is_child_container():
            # Parent-only: spawn/manage children and rollback parent code.
            from app.tools.spawn import spawn_agent, list_spawned_agents, stop_spawned_agent
            tools.extend([
                FunctionTool(func=trigger_rollback),
                FunctionTool(func=spawn_agent),
                FunctionTool(func=list_spawned_agents),
                FunctionTool(func=stop_spawned_agent),
            ])

        return tools
