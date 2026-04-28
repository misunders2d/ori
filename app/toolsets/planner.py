from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class PlannerToolset(BaseToolset):
    """Plan creation, traversal, completion, and inspection.

    The agent drives the step loop directly via `get_next_step` and
    `complete_step` (legacy nudge-based pattern). The scheduled-task
    runner's `_drive_plan_to_completion` re-invokes the agent with a
    continuation prompt while pending steps remain — so even if the LLM
    forgets to call `get_next_step` after a step, the next turn nudges
    it back.
    """

    async def get_tools(self, readonly_context=None):
        from app.tools.planner import (
            abandon_plan,
            complete_step,
            create_plan,
            get_next_step,
            get_plan_status,
        )

        return [
            FunctionTool(func=create_plan),
            FunctionTool(func=get_plan_status),
            FunctionTool(func=get_next_step),
            FunctionTool(func=complete_step),
            FunctionTool(func=abandon_plan),
        ]
