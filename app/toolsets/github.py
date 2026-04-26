from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class GitHubToolset(BaseToolset):
    """Native GitHub REST API tools — view repos, issues, files, create PRs, search code."""

    async def get_tools(self, readonly_context=None):
        from app.tools.github_api import (
            github_create_pr,
            github_list_issues,
            github_list_repos,
            github_search_code,
            github_view_file,
        )

        return [
            FunctionTool(func=github_list_issues),
            FunctionTool(func=github_view_file),
            FunctionTool(func=github_create_pr),
            FunctionTool(func=github_list_repos),
            FunctionTool(func=github_search_code),
        ]
