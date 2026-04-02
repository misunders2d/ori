from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class OAuthToolset(BaseToolset):
    """Groups OAuth2 platform connection tools."""

    async def get_tools(self, readonly_context=None):
        from app.tools.auth import (
            list_platforms,
            register_platform,
            connect_to_platform,
            complete_auth_code,
            check_connection,
            disconnect_platform,
            remove_platform_registration,
        )

        return [
            FunctionTool(func=list_platforms),
            FunctionTool(func=register_platform),
            FunctionTool(func=connect_to_platform),
            FunctionTool(func=complete_auth_code),
            FunctionTool(func=check_connection),
            FunctionTool(func=disconnect_platform),
            FunctionTool(func=remove_platform_registration),
        ]
