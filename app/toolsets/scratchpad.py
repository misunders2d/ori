from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class ScratchpadToolset(BaseToolset):
    """File-based scratchpad for multi-step research tasks."""

    async def get_tools(self, readonly_context=None):
        from app.tools.scratchpad import (
            scratchpad_clear,
            scratchpad_list,
            scratchpad_read,
            scratchpad_replace,
            scratchpad_write,
        )

        return [
            FunctionTool(func=scratchpad_write),
            FunctionTool(func=scratchpad_read),
            FunctionTool(func=scratchpad_replace),
            FunctionTool(func=scratchpad_clear),
            FunctionTool(func=scratchpad_list),
        ]
