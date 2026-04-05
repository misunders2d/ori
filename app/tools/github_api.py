"""Native GitHub REST API tools — replaces MCP GitHub bridge.

All operations use httpx + GitHub REST API v3. Auth via GITHUB_TOKEN
from vault/os.environ. No Node.js, no MCP, no external processes.
"""

import logging
import os

import httpx
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"


def _headers() -> dict:
    """Build GitHub API headers with auth token if available."""
    token = os.environ.get("GITHUB_TOKEN", "")
    h = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Ori-Agent",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def github_list_issues(repo: str, state: str, tool_context: ToolContext) -> dict:
    """Lists issues and pull requests from a GitHub repository.

    Args:
        repo: Repository in 'owner/repo' format (e.g. 'google/adk-python').
        state: Filter by state. One of: open, closed, all.

    Returns:
        dict: List of issues with number, title, state, and labels.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_API_BASE}/repos/{repo}/issues",
                headers=_headers(),
                params={"state": state, "per_page": 20},
            )
            resp.raise_for_status()
            issues = [
                {
                    "number": i["number"],
                    "title": i["title"],
                    "state": i["state"],
                    "is_pr": "pull_request" in i,
                    "labels": [l["name"] for l in i.get("labels", [])],
                    "created_at": i["created_at"],
                }
                for i in resp.json()
            ]
            return {"status": "success", "repo": repo, "issues": issues, "count": len(issues)}
    except Exception as e:
        return {"status": "error", "message": f"GitHub API error: {e}"}


async def github_view_file(repo: str, path: str, tool_context: ToolContext, ref: str = "") -> dict:
    """Reads the content of a file from a GitHub repository.

    Args:
        repo: Repository in 'owner/repo' format.
        path: File path within the repo (e.g. 'src/main.py').
        ref: Optional branch, tag, or commit SHA. Defaults to the repo's default branch.

    Returns:
        dict: File content as text.
    """
    try:
        params = {}
        if ref:
            params["ref"] = ref
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_API_BASE}/repos/{repo}/contents/{path}",
                headers=_headers(),
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("encoding") == "base64":
                import base64
                content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            else:
                content = data.get("content", "")
            return {
                "status": "success",
                "repo": repo,
                "path": path,
                "content": content[:15000],  # Cap to prevent token explosion
                "size": data.get("size", 0),
                "sha": data.get("sha", ""),
            }
    except Exception as e:
        return {"status": "error", "message": f"GitHub API error: {e}"}


async def github_create_pr(repo: str, title: str, body: str, head: str, base: str, tool_context: ToolContext) -> dict:
    """Creates a pull request on a GitHub repository.

    Args:
        repo: Repository in 'owner/repo' format.
        title: PR title.
        body: PR description (markdown).
        head: Branch containing changes.
        base: Branch to merge into (e.g. 'main' or 'master').

    Returns:
        dict: PR number and URL.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_API_BASE}/repos/{repo}/pulls",
                headers=_headers(),
                json={"title": title, "body": body, "head": head, "base": base},
            )
            resp.raise_for_status()
            pr = resp.json()
            return {
                "status": "success",
                "number": pr["number"],
                "url": pr["html_url"],
                "state": pr["state"],
            }
    except Exception as e:
        return {"status": "error", "message": f"GitHub API error: {e}"}


async def github_list_repos(owner: str, tool_context: ToolContext) -> dict:
    """Lists repositories for a user or organization.

    Args:
        owner: GitHub username or organization name.

    Returns:
        dict: List of repositories with name, description, and URL.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_API_BASE}/users/{owner}/repos",
                headers=_headers(),
                params={"per_page": 30, "sort": "updated"},
            )
            resp.raise_for_status()
            repos = [
                {
                    "name": r["full_name"],
                    "description": r.get("description", ""),
                    "url": r["html_url"],
                    "language": r.get("language"),
                    "updated_at": r["updated_at"],
                }
                for r in resp.json()
            ]
            return {"status": "success", "owner": owner, "repos": repos, "count": len(repos)}
    except Exception as e:
        return {"status": "error", "message": f"GitHub API error: {e}"}


async def github_search_code(query: str, tool_context: ToolContext) -> dict:
    """Searches code across GitHub repositories.

    Args:
        query: Search query. Can include qualifiers like 'repo:owner/name', 'language:python', 'path:src/'.

    Returns:
        dict: Search results with file path, repo, and text matches.
    """
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_API_BASE}/search/code",
                headers=_headers(),
                params={"q": query, "per_page": 15},
            )
            resp.raise_for_status()
            data = resp.json()
            results = [
                {
                    "repo": item["repository"]["full_name"],
                    "path": item["path"],
                    "url": item["html_url"],
                    "score": item.get("score", 0),
                }
                for item in data.get("items", [])
            ]
            return {"status": "success", "query": query, "results": results, "total": data.get("total_count", 0)}
    except Exception as e:
        return {"status": "error", "message": f"GitHub API error: {e}"}
