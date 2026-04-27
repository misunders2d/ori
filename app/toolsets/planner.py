from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class PlannerToolset(BaseToolset):
    """Plan creation/inspection. Step execution is owned by the workflow.

    `get_next_step` and `complete_step` are NOT exposed as agent tools —
    the plan_executor workflow drives the step loop deterministically
    (see `app/workflows/plan_executor.py`). The agent only creates plans,
    inspects status, or abandons. Step ordering and completion are
    enforced by code, not by LLM compliance.
    """

    async def get_tools(self, readonly_context=None):
        from app.tools.planner import (
            abandon_plan,
            create_plan,
            get_plan_status,
        )

        return [
            FunctionTool(func=create_plan),
            FunctionTool(func=get_plan_status),
            FunctionTool(func=abandon_plan),
        ]
