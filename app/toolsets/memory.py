from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class MemoryToolset(BaseToolset):
    """Groups long-term memory and user preference tools."""

    async def get_tools(self, readonly_context=None):
        from app.tools.memory import (
            remember_info,
            search_memory,
            recall_human_preferences,
            recall_technical_context,
        )
        from app.tools.preferences import (
            save_user_preferences,
            get_user_preferences,
        )

        # Note: modify_memory / delete_memory are intentionally not exposed.
        # BaseMemoryService doesn't define update/delete; the convention is to
        # write a new entry tagging the original as superseded.
        # See app/tools/memory.py for the design note.
        return [
            FunctionTool(func=remember_info),
            FunctionTool(func=search_memory),
            FunctionTool(func=recall_human_preferences),
            FunctionTool(func=recall_technical_context),
            FunctionTool(func=save_user_preferences),
            FunctionTool(func=get_user_preferences),
        ]
