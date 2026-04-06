from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class SPApiToolset(BaseToolset):
    """Amazon Selling Partner API — catalog, listings, pricing, reports."""

    async def get_tools(self, readonly_context=None):
        from app.tools.sp_api_tools import (
            sp_get_catalog_item,
            sp_search_catalog,
            sp_get_listing,
            sp_get_competitive_pricing,
            sp_request_report,
            sp_check_report,
            sp_download_report,
            sp_list_reports,
        )

        return [
            FunctionTool(func=sp_get_catalog_item),
            FunctionTool(func=sp_search_catalog),
            FunctionTool(func=sp_get_listing),
            FunctionTool(func=sp_get_competitive_pricing),
            FunctionTool(func=sp_request_report),
            FunctionTool(func=sp_check_report),
            FunctionTool(func=sp_download_report),
            FunctionTool(func=sp_list_reports),
        ]
