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

        return [
            FunctionTool(func=update_self),
            FunctionTool(func=session_refresh),
            FunctionTool(func=trigger_rollback),
            FunctionTool(func=set_planner_mode),
            FunctionTool(func=execute_approved_action),
            FunctionTool(func=report_health),
            FunctionTool(func=inspect_secure_env),
        ]
