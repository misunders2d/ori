import os
import logging
from app.core.health import get_system_health
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

def check_active_tasks(tool_context: ToolContext) -> dict:
    """Check background task status."""
    try:
        from app.tasks import ACTIVE_TASKS
        if not ACTIVE_TASKS: return {"status": "success", "message": "No active tasks."}
        task_list = []
        for tid, data in ACTIVE_TASKS.items():
            task_list.append({
                "task_id": tid, "type": data.get("type"), "status": data.get("status"),
                "start_time": data.get("start_time"), "prompt": data.get("prompt")
            })
        return {"status": "success", "active_tasks": task_list}
    except Exception: return {"status": "error", "message": "Task check failed."}

def repair_data_permissions(tool_context: ToolContext = None) -> dict:
    """Fixes 'readonly database' by recursively correcting permissions in data/."""
    data_dir = os.path.abspath("./data")
    try:
        # 777 for directories, 666 for files (Definitive fix for Docker permission sync)
        os.chmod(data_dir, 0o777)
        count = 0
        for root, dirs, files in os.walk(data_dir):
            for d in dirs: 
                try: os.chmod(os.path.join(root, d), 0o777)
                except Exception: pass
            for f in files:
                f_path = os.path.join(root, f)
                try: os.chmod(f_path, 0o666)
                except Exception: pass
                count += 1
        return {"status": "success", "message": f"Repaired {count} files in data/."}
    except Exception as e: return {"status": "error", "message": str(e)}

async def report_health(tool_context: ToolContext) -> dict:
    """Returns a full system health report including API, disk, and git integrity."""
    try:
        report = await get_system_health()
        return {"status": "success", "health": report}
    except Exception as e:
        return {"status": "error", "message": f"Health check failed: {str(e)}"}

def inspect_secure_env(tool_context: ToolContext) -> dict:
    """Lists environment variables with sensitive values redacted."""
    from app.app_utils.config import ALLOWED_CONFIG_KEYS
    
    redacted_env = {}
    for key, val in os.environ.items():
        if key in ALLOWED_CONFIG_KEYS or "SECRET" in key or "TOKEN" in key or "KEY" in key or "PASSCODE" in key:
            if val:
                redacted_env[key] = f"{val[:3]}...{val[-3:]}" if len(val) > 10 else "[REDACTED]"
            else:
                redacted_env[key] = None
        else:
            redacted_env[key] = val
            
    return {"status": "success", "environment": redacted_env}
