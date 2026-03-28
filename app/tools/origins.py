"""Tools for comparing and adopting improvements from the upstream Ori repository."""

import logging
from typing import Dict, Any, List
from google.adk.tools.tool_context import ToolContext
from app.core.origins import get_upstream_status, get_file_diff

logger = logging.getLogger(__name__)

async def check_upstream(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Checks the official Ori repository (upstream) for new features, security fixes, or code improvements.
    Compares the remote code with the local evolved codebase and reports the findings.
    """
    try:
        status = await get_upstream_status()
        
        if status["status"] == "error":
            return {
                "status": "error",
                "message": status["message"]
            }
            
        if status["status"] == "synced":
            return {
                "status": "success",
                "message": "✅ **Origins Report**: You are running the latest version of the Ori framework. No upstream changes detected."
            }
            
        # Format a proposal message
        msg = f"💡 **Origins Report**: New improvements found in the upstream Ori repository!\n\n"
        msg += f"**Status:** {status['message']}\n\n"
        
        if status.get("commits"):
            msg += "**Recent Upstream Commits:**\n"
            for commit in status["commits"][:5]:
                msg += f"- {commit}\n"
            if len(status["commits"]) > 5:
                msg += f"*(...and {len(status['commits']) - 5} more)*\n"
                
        if status.get("new_files"):
            msg += "\n**New Upstream Files:**\n"
            for file in status["new_files"][:5]:
                msg += f"- `{file}`\n"
            if len(status["new_files"]) > 5:
                msg += f"*(...and {len(status['new_files']) - 5} more)*\n"
                
        msg += "\nTo adopt specific changes, you can ask the Developer Agent to 'analyze upstream' for a particular feature or file."
        
        return {
            "status": "success",
            "message": msg,
            "data": status
        }
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"Upstream check failed: {str(e)}"
        }

async def analyze_upstream_file(file_path: str, tool_context: ToolContext) -> Dict[str, Any]:
    """
    Retrieves the raw diff for a specific file between the local codebase and upstream.
    Use this to inspect a proposed improvement before adopting it.
    """
    diff = await get_file_diff(file_path)
    
    if not diff:
        return {
            "status": "success",
            "message": f"No differences found for `{file_path}` between local and upstream master."
        }
        
    if "Error" in diff:
        return {
            "status": "error",
            "message": diff
        }
        
    return {
        "status": "success",
        "message": f"📄 **Upstream Diff for `{file_path}`**:\n\n```diff\n{diff[:3000]}\n```",
        "full_diff": diff
    }
