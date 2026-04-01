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
        # We mock this in tests, but in production we need a real check.
        # Use a minimal call.
        client = genai.Client()
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
        # 4a. Check for local changes in tracked files
        diff_res = subprocess.run(
            ["git", "diff-index", "--quiet", "HEAD", "--"],
            capture_output=True, text=True, timeout=5
        )
        is_modified = diff_res.returncode != 0
        
        # 4b. Identify branch and local hash
        branch_res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        branch = branch_res.stdout.strip() or "master"
        
        head_res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        local_hash = head_res.stdout.strip()
        report["vitals"]["version_hash"] = local_hash

        # 4c. Compare with remote tracking branch
        remote_ref = f"origin/{branch}"
        remote_res = subprocess.run(
            ["git", "rev-parse", "--short", remote_ref],
            capture_output=True, text=True, timeout=5
        )
        
        integrity = "unknown"
        if remote_res.returncode == 0:
            remote_hash = remote_res.stdout.strip()
            if local_hash == remote_hash:
                integrity = "synced"
            else:
                count_res = subprocess.run(
                    ["git", "rev-list", "--left-right", "--count", f"{local_hash}...{remote_hash}"],
                    capture_output=True, text=True, timeout=5
                )
                if count_res.returncode == 0:
                    counts = count_res.stdout.strip().split()
                    if len(counts) == 2:
                        ahead, behind = counts
                        if ahead != "0" and behind == "0":
                            integrity = f"ahead_by_{ahead}"
                        elif ahead == "0" and behind != "0":
                            integrity = f"behind_by_{behind}"
                        elif ahead != "0" and behind != "0":
                            integrity = "diverged"
                    else:
                        integrity = "out_of_sync"
                else:
                    integrity = "out_of_sync"
        else:
            integrity = "local_only"

        if is_modified:
            integrity += " (modified)"
            
        report["vitals"]["git_integrity"] = integrity
            
    except Exception:
        report["vitals"]["git_integrity"] = "unknown"

    return report
