from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class KeepaToolset(BaseToolset):
    """Amazon product research tools via Keepa API."""

    async def get_tools(self, readonly_context=None):
        from app.tools.keepa_api import (
            keepa_get_product_data,
            keepa_product_finder,
            keepa_get_categories,
            keepa_get_bestsellers,
            keepa_get_seller_info,
            keepa_get_top_sellers,
        )

        return [
            FunctionTool(func=keepa_get_product_data),
            FunctionTool(func=keepa_product_finder),
            FunctionTool(func=keepa_get_categories),
            FunctionTool(func=keepa_get_bestsellers),
            FunctionTool(func=keepa_get_seller_info),
            FunctionTool(func=keepa_get_top_sellers),
        ]
