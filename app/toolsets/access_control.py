from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class AccessControlToolset(BaseToolset):
    """Groups whitelist/blacklist access control tools."""

    async def get_tools(self, readonly_context=None):
        from app.tools.whitelist import (
            whitelist_chat,
            blacklist_chat,
            unwhitelist_chat,
            list_access_control,
        )

        return [
            FunctionTool(func=whitelist_chat),
            FunctionTool(func=blacklist_chat),
            FunctionTool(func=unwhitelist_chat),
            FunctionTool(func=list_access_control),
        ]
