import os

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _is_child_container() -> bool:
    """Detect if we're running as a spawned child (no .git, no Docker)."""
    return not os.path.isdir(os.path.join(_PROJECT_ROOT, ".git"))


class SystemToolset(BaseToolset):
    """Core system lifecycle tools."""

    async def get_tools(self, readonly_context=None):
        from app.tools.system import (
            execute_approved_action,
            session_refresh,
            set_planner_mode,
            trigger_rollback,
            update_self,
        )

        tools = [
            FunctionTool(func=session_refresh),
            FunctionTool(func=set_planner_mode),
            FunctionTool(func=execute_approved_action),
        ]

        if not _is_child_container():
            # Parent-only: spawn/manage children, reboot, rollback
            from app.tools.spawn import (
                list_spawned_agents,
                spawn_agent,
                stop_spawned_agent,
            )
            tools.extend([
                FunctionTool(func=update_self),
                FunctionTool(func=trigger_rollback),
                FunctionTool(func=spawn_agent),
                FunctionTool(func=list_spawned_agents),
                FunctionTool(func=stop_spawned_agent),
            ])

        return tools
