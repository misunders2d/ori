from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class H10Toolset(BaseToolset):
    """Helium10 keyword analysis — analyze Cerebro and Magnet exports."""

    async def get_tools(self, readonly_context=None):
        from app.tools.h10_analysis import (
            analyze_keywords,
            find_keyword_gaps,
            find_trending_keywords,
            find_long_tail_opportunities,
            keyword_score_report,
            keyword_summary,
        )

        return [
            FunctionTool(func=keyword_summary),
            FunctionTool(func=analyze_keywords),
            FunctionTool(func=find_keyword_gaps),
            FunctionTool(func=find_trending_keywords),
            FunctionTool(func=find_long_tail_opportunities),
            FunctionTool(func=keyword_score_report),
        ]
