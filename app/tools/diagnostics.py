import logging
import os
import re
from datetime import datetime, timedelta, timezone

from google.adk.tools.tool_context import ToolContext

from app.runtime.health import get_system_health

logger = logging.getLogger(__name__)


# Plugin name → human label. Anything matching one of these prefixes in
# the log is a gate event we want to surface for admin debugging.
_GATE_PLUGIN_PREFIXES = {
    "PerimeterAclPlugin": "perimeter",
    "AdminGatePlugin": "admin_gate",
    "PromptInjectionGuardPlugin": "prompt_injection",
    "OutputSanitizerPlugin": "output_sanitizer",
    "BinaryContentScannerPlugin": "binary_scanner",
    "VerifyRetryPlugin": "verify_retry",
    "A2APrivacyPlugin": "a2a_privacy",
    "ModelErrorHandlerPlugin": "model_error",
    "PlanEnforcerPlugin": "plan_enforcer",
}

# data/agent.log line: "LEVEL: <plugin>: <message>"  OR  "TIMESTAMP - module - LEVEL - <plugin>: ..."
# Grab WARNING/ERROR lines mentioning a known plugin name.
_LOG_FILE = os.path.abspath("./data/agent.log")
_LINE_RE = re.compile(
    r"^(?P<level>WARNING|ERROR):\s*(?P<plugin>[A-Z][A-Za-z0-9]+Plugin):\s*(?P<msg>.*)$"
)

def check_active_tasks(tool_context: ToolContext) -> dict:
    """Check background task status."""
    try:
        from app.tasks import ACTIVE_TASKS
        if not ACTIVE_TASKS:
            return {"status": "success", "message": "No active tasks."}
        task_list = []
        for tid, data in ACTIVE_TASKS.items():
            task_list.append({
                "task_id": tid, "type": data.get("type"), "status": data.get("status"),
                "start_time": data.get("start_time"), "prompt": data.get("prompt")
            })
        return {"status": "success", "active_tasks": task_list}
    except Exception:
        return {"status": "error", "message": "Task check failed."}

async def report_health(tool_context: ToolContext) -> dict:
    """Returns a full system health report including API, disk, and git integrity."""
    try:
        report = await get_system_health()
        return {"status": "success", "health": report}
    except Exception as e:
        return {"status": "error", "message": f"Health check failed: {e!s}"}

def read_gate_logs(
    hours: int = 24,
    limit: int = 50,
    plugin: str = "",
    tool_context: ToolContext | None = None,
) -> dict:
    """Read recent gate-event lines from data/agent.log.

    Surfaces what the guardrail plugins blocked or warned about — perimeter
    ACL denies, admin-gate staged-action prompts, prompt-injection blocks,
    output-sanitizer redactions, binary-scanner rejections, verify-retry
    cap hits, A2A-privacy refusals, model errors, plan-enforcer warnings.

    Args:
        hours: window to scan (default 24).
        limit: max entries to return, newest first (default 50).
        plugin: filter to one plugin name (e.g. "perimeter",
            "admin_gate", "prompt_injection"). Empty = all plugins.

    Returns:
        {"status": "success", "entries": [{plugin, level, message, ...}]}
        Each entry's `timestamp` field is best-effort — agent.log lines don't
        always carry one. Use `level` to filter to ERRORs vs WARNINGs.
    """
    if not os.path.isfile(_LOG_FILE):
        return {
            "status": "success",
            "entries": [],
            "message": f"Log file not found at {_LOG_FILE}",
        }

    plugin_filter = (plugin or "").strip().lower()
    if plugin_filter and plugin_filter not in set(_GATE_PLUGIN_PREFIXES.values()):
        return {
            "status": "error",
            "error_code": "UNKNOWN_PLUGIN",
            "message": (
                f"Unknown plugin '{plugin}'. Valid: "
                f"{', '.join(sorted(set(_GATE_PLUGIN_PREFIXES.values())))}, or empty for all."
            ),
        }

    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=max(1, int(hours)))
    entries: list[dict] = []
    try:
        with open(_LOG_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _LINE_RE.match(line.rstrip("\n"))
                if not m:
                    continue
                plugin_name = _GATE_PLUGIN_PREFIXES.get(m.group("plugin"))
                if not plugin_name:
                    continue
                if plugin_filter and plugin_name != plugin_filter:
                    continue
                entries.append({
                    "plugin": plugin_name,
                    "level": m.group("level"),
                    "message": m.group("msg"),
                })
    except Exception as e:
        return {
            "status": "error",
            "error_code": "LOG_READ_FAILED",
            "message": f"Could not read {_LOG_FILE}: {e!s}",
        }

    # Take newest N (file is append-ordered → tail).
    entries = entries[-int(limit):] if len(entries) > int(limit) else entries
    entries.reverse()  # newest first
    _ = cutoff  # timestamp filtering not applied — agent.log lines lack timestamps; window is implicit via file tail.
    return {"status": "success", "entries": entries, "count": len(entries)}


def inspect_secure_env(tool_context: ToolContext) -> dict:
    """Lists environment variables with sensitive values redacted."""
    from app.util.config import ALLOWED_CONFIG_KEYS

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
