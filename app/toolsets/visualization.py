from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class VisualizationToolset(BaseToolset):
    """Data visualization — generates charts from Python plotting code."""

    async def get_tools(self, readonly_context=None):
        from app.tools.visualize import generate_chart

        return [
            FunctionTool(func=generate_chart),
        ]
