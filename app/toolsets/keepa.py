from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class KeepaToolset(BaseToolset):
    """Amazon product research tools via Keepa API — fetch-store-extract pattern."""

    async def get_tools(self, readonly_context=None):
        from app.tools.keepa_api import (
            # Token management
            keepa_check_tokens,
            # Fetch (API call → cache)
            keepa_fetch_product,
            # Extract (cache → focused data)
            keepa_extract_pricing,
            keepa_extract_history,
            keepa_extract_offers,
            keepa_extract_stats,
            keepa_extract_competitors,
            # Other endpoints
            keepa_product_finder,
            keepa_get_categories,
            keepa_get_bestsellers,
            keepa_get_seller_info,
            keepa_get_top_sellers,
        )

        return [
            FunctionTool(func=keepa_check_tokens),
            FunctionTool(func=keepa_fetch_product),
            FunctionTool(func=keepa_extract_pricing),
            FunctionTool(func=keepa_extract_history),
            FunctionTool(func=keepa_extract_offers),
            FunctionTool(func=keepa_extract_stats),
            FunctionTool(func=keepa_extract_competitors),
            FunctionTool(func=keepa_product_finder),
            FunctionTool(func=keepa_get_categories),
            FunctionTool(func=keepa_get_bestsellers),
            FunctionTool(func=keepa_get_seller_info),
            FunctionTool(func=keepa_get_top_sellers),
        ]
