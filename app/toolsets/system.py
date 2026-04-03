from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class SystemToolset(BaseToolset):
    """Groups system lifecycle, health, and admin tools."""

    async def get_tools(self, readonly_context=None):
        from app.tools.system import (
            update_self,
            session_refresh,
            trigger_rollback,
            set_planner_mode,
            execute_approved_action,
        )
        from app.tools.diagnostics import report_health, inspect_secure_env
        from app.tools.model_tools import list_available_models, set_agent_model
        from app.tools.spawn import spawn_agent, list_spawned_agents, stop_spawned_agent

        return [
            FunctionTool(func=update_self),
            FunctionTool(func=session_refresh),
            FunctionTool(func=trigger_rollback),
            FunctionTool(func=set_planner_mode),
            FunctionTool(func=execute_approved_action),
            FunctionTool(func=report_health),
            FunctionTool(func=inspect_secure_env),
            FunctionTool(func=list_available_models),
            FunctionTool(func=set_agent_model),
            FunctionTool(func=spawn_agent),
            FunctionTool(func=list_spawned_agents),
            FunctionTool(func=stop_spawned_agent),
        ]
