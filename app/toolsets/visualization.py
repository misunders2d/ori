from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class VisualizationToolset(BaseToolset):
    """Data visualization and file export — charts, CSVs, Excel, PDFs."""

    async def get_tools(self, readonly_context=None):
        from app.tools.visualize import generate_chart
        from app.tools.export_file import generate_file

        return [
            FunctionTool(func=generate_chart),
            FunctionTool(func=generate_file),
        ]
