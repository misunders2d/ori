from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class PlannerToolset(BaseToolset):
    """Structured plan-and-execute — create plans, execute steps sequentially."""

    async def get_tools(self, readonly_context=None):
        from app.tools.planner import (
            create_plan,
            get_next_step,
            complete_step,
            get_plan_status,
            abandon_plan,
        )

        return [
            FunctionTool(func=create_plan),
            FunctionTool(func=get_next_step),
            FunctionTool(func=complete_step),
            FunctionTool(func=get_plan_status),
            FunctionTool(func=abandon_plan),
        ]
