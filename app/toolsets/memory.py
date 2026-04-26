from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class MemoryToolset(BaseToolset):
    """Groups long-term memory and user preference tools."""

    async def get_tools(self, readonly_context=None):
        from app.tools.memory import (
            delete_memory,
            modify_memory,
            recall_human_preferences,
            recall_technical_context,
            remember_info,
            search_memory,
        )
        from app.tools.preferences import (
            get_user_preferences,
            save_user_preferences,
        )

        # modify_memory / delete_memory are OriMemoryService extensions —
        # BaseMemoryService doesn't define update/delete, so the tools call
        # the underlying service directly via tool_context.invocation_context.
        return [
            FunctionTool(func=remember_info),
            FunctionTool(func=search_memory),
            FunctionTool(func=modify_memory),
            FunctionTool(func=delete_memory),
            FunctionTool(func=recall_human_preferences),
            FunctionTool(func=recall_technical_context),
            FunctionTool(func=save_user_preferences),
            FunctionTool(func=get_user_preferences),
        ]
