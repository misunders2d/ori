from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class IntegrationToolset(BaseToolset):
    """Groups integration configuration tools."""

    async def get_tools(self, readonly_context=None):
        from app.tools.integrations import (
            configure_integration,
            list_integrations,
            remove_integration,
        )

        return [
            FunctionTool(func=configure_integration),
            FunctionTool(func=remove_integration),
            FunctionTool(func=list_integrations),
        ]
