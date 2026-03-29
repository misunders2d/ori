"""Tools for researching bugs, library docs, and GitHub issues."""

import logging
import os
import subprocess
import sys

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


def search_github_issues(
    query: str, repo: str, tool_context: ToolContext, state: str = "all", max_results: int = 5
) -> dict:
    """Searches GitHub Issues and Discussions for a specific repository.

    Use this when you hit an error or unexpected behavior with a library.
    Search the library's repo for similar issues, workarounds, or confirmed bugs.

    Args:
        query (str): The search query — an error message, keyword, or description of the problem.
        repo (str): The GitHub repository to search (e.g., 'google/adk-python', 'pallets/flask').
        state (str): Filter by issue state: 'open', 'closed', or 'all'. Default: 'all'.
        max_results (int): Maximum number of results to return. Default: 5.

    Returns:
        dict: Matching issues with titles, URLs, labels, and top comments.
    """
    import httpx

    github_token = os.environ.get("GITHUB_TOKEN", "")
    headers = {"Accept": "application/vnd.github.v3+json"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    # Use GitHub search API: scope to the repo and include issue state
    search_query = f"{query} repo:{repo}"
    if state in ("open", "closed"):
        search_query += f" state:{state}"

    try:
        with httpx.Client(timeout=20.0, headers=headers) as client:
            resp = client.get(
                "https://api.github.com/search/issues",
                params={"q": search_query, "per_page": max_results, "sort": "relevance"},
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("total_count", 0) == 0:
                return {
                    "status": "success",
                    "message": f"No issues found in {repo} matching: {query}",
                    "results": [],
                }

            results = []
            for item in data.get("items", [])[:max_results]:
                issue = {
                    "title": item.get("title"),
                    "url": item.get("html_url"),
                    "state": item.get("state"),
                    "labels": [l["name"] for l in item.get("labels", [])],
                    "created": item.get("created_at", "")[:10],
                    "body_preview": (item.get("body") or "")[:500],
                }
                results.append(issue)

            return {
                "status": "success",
                "message": f"Found {data['total_count']} issues in {repo}. Showing top {len(results)}.",
                "results": results,
            }

    except Exception as e:
        return {"status": "error", "message": f"GitHub search failed: {e}"}


def check_installed_package(package_name: str, tool_context: ToolContext) -> dict:
    """Checks the installed version and metadata of a Python package.

    Use this to verify which version of a library is actually installed before
    writing code against it. This prevents coding against the wrong API version.

    Args:
        package_name (str): The pip package name (e.g., 'google-adk', 'flask', 'httpx').

    Returns:
        dict: Package version, location, dependencies, and summary.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", package_name],
            capture_output=True, text=True, timeout=15,
        )

        if result.returncode != 0:
            return {
                "status": "error",
                "message": f"Package '{package_name}' is not installed.",
            }

        # Parse pip show output into a dict
        info = {}
        for line in result.stdout.strip().splitlines():
            if ": " in line:
                key, _, value = line.partition(": ")
                info[key.strip()] = value.strip()

        return {
            "status": "success",
            "package": package_name,
            "version": info.get("Version", "unknown"),
            "summary": info.get("Summary", ""),
            "location": info.get("Location", ""),
            "requires": info.get("Requires", ""),
            "required_by": info.get("Required-by", ""),
        }

    except Exception as e:
        return {"status": "error", "message": f"Failed to check package: {e}"}
