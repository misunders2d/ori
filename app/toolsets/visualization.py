from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class VisualizationToolset(BaseToolset):
    """Data visualization and file export — charts, CSVs, Excel, PDFs."""

    async def get_tools(self, readonly_context=None):
        from app.tools.visualize import generate_chart
        from app.tools.export_file import generate_file
        from app.tools.analyze_data import analyze_data

        return [
            FunctionTool(func=generate_chart),
            FunctionTool(func=generate_file),
            FunctionTool(func=analyze_data),
        ]


class CreativesToolset(BaseToolset):
    """AI image generation and editing — text-to-image, image-to-image, prompt enhancement."""

    async def get_tools(self, readonly_context=None):
        from app.tools.genai_image import generate_image, enhance_image_prompt

        return [
            FunctionTool(func=generate_image),
            FunctionTool(func=enhance_image_prompt),
        ]
