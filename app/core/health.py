import os
import shutil
import subprocess
from datetime import datetime
from typing import Dict, Any

from google import genai
from interfaces.telegram_poller import HEARTBEAT_FILE

async def get_system_health() -> Dict[str, Any]:
    """Compiles a comprehensive health report of the agent's vitals."""
    report = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "vitals": {}
    }

    # 1. Google API Health
    try:
        client = genai.Client()
        # Simple non-destructive call to verify key and connectivity
        await client.aio.models.list(config={'page_size': 1})
        report["vitals"]["google_api"] = "online"
    except Exception as e:
        report["vitals"]["google_api"] = f"error: {str(e)}"
        report["status"] = "degraded"

    # 2. Telegram Poller Liveness
    if os.path.exists(HEARTBEAT_FILE):
        try:
            with open(HEARTBEAT_FILE, "r") as f:
                last_heartbeat = datetime.fromisoformat(f.read().strip())
                diff = (datetime.now() - last_heartbeat).total_seconds()
                if diff < 60:
                    report["vitals"]["telegram_poller"] = "active"
                else:
                    report["vitals"]["telegram_poller"] = f"stalled ({int(diff)}s ago)"
                    report["status"] = "degraded"
        except Exception:
            report["vitals"]["telegram_poller"] = "heartbeat_corrupt"
    else:
        report["vitals"]["telegram_poller"] = "not_started"

    # 3. Disk Usage
    data_dir = os.path.abspath("./data")
    if os.path.exists(data_dir):
        total, used, free = shutil.disk_usage(data_dir)
        percent_used = (used / total) * 100
        report["vitals"]["disk_usage"] = f"{percent_used:.1f}% used"
        if percent_used > 90:
            report["status"] = "degraded"

    # 4. Git Status (File Integrity / Drift)
    try:
        # Check for local changes
        result = subprocess.run(
            ["git", "status", "--short"], 
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            report["vitals"]["git_integrity"] = "modified_locally"
        else:
            report["vitals"]["git_integrity"] = "synced"
            
        # Check for upstream drift (requires fetch)
        # Note: we don't fetch here to avoid network lag, just report HEAD
        head_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        report["vitals"]["version_hash"] = head_result.stdout.strip()
    except Exception:
        report["vitals"]["git_integrity"] = "unknown"

    return report
