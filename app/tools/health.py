"""Tool for manual system health reporting."""

from typing import Dict, Any
from google.adk.tools.tool_context import ToolContext
from app.core.health import get_system_health

async def report_health(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Performs a self-diagnostic check on API connectivity, poller liveness, 
    disk usage, and git integrity. Returns a detailed health report.
    """
    try:
        report = await get_system_health()
        
        status_emoji = "✅" if report["status"] == "healthy" else "⚠️"
        
        msg = f"{status_emoji} **System Health Report**\n\n"
        msg += f"**Status:** {report['status'].upper()}\n"
        msg += f"**Timestamp:** {report['timestamp']}\n\n"
        
        msg += "**Vitals:**\n"
        for key, value in report["vitals"].items():
            msg += f"- {key.replace('_', ' ').capitalize()}: `{value}`\n"
            
        return {
            "status": "success",
            "message": msg,
            "data": report
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Diagnostics failed: {str(e)}"
        }
