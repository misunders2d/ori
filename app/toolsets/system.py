from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class SystemToolset(BaseToolset):
    """Core system lifecycle tools."""

    async def get_tools(self, readonly_context=None):
        from app.tools.system import (
            update_self,
            session_refresh,
            trigger_rollback,
            set_planner_mode,
            execute_approved_action,
        )
        from app.tools.spawn import spawn_agent, list_spawned_agents, stop_spawned_agent

        return [
            FunctionTool(func=update_self),
            FunctionTool(func=session_refresh),
            FunctionTool(func=trigger_rollback),
            FunctionTool(func=set_planner_mode),
            FunctionTool(func=execute_approved_action),
            FunctionTool(func=spawn_agent),
            FunctionTool(func=list_spawned_agents),
            FunctionTool(func=stop_spawned_agent),
        ]
