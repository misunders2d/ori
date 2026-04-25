"""A2APrivacyPlugin — block credential leaks in outbound A2A traffic.

Scans the args of A2A-relevant tool calls (and their responses) for any
substring that matches a known secret in vault / env. Blocks the call/
response if a match is found.

Scoped to A2A-risk tools so non-A2A tools aren't paying the scan cost on
every call. The list is conservative: anything that crosses Ori's network
boundary outbound or packages files for export.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from google.adk.plugins import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from app.util.config import ALLOWED_CONFIG_KEYS

logger = logging.getLogger(__name__)


# Tools whose args / outputs may carry data outside Ori's local boundary.
_A2A_RISK_TOOLS: frozenset[str] = frozenset({
    "call_friend",
    "call_agent",
    "export_dna",
    "add_friend",
    "web_fetch",
})

# Public-ish keys that are safe to share via DNA (or are not credentials).
_SAFE_KEYS: frozenset[str] = frozenset({
    "BOT_NAME",
    "GITHUB_REPO",
    "APP_NAME",
})

# Extra credential keys not in ALLOWED_CONFIG_KEYS but still must not leak.
_EXTRA_SECRET_KEYS: tuple[str, ...] = ("ADMIN_PASSCODE", "ADMIN_TOTP_SECRET")

# Minimum length for a value to be considered a secret. Below this, false
# positives dominate (single-char or short tokens).
_MIN_SECRET_LEN = 7


def _collect_secrets() -> list[str]:
    """Materialize the set of secret values currently in env."""
    secrets: list[str] = []
    for key in ALLOWED_CONFIG_KEYS:
        if key in _SAFE_KEYS:
            continue
        val = os.environ.get(key)
        if val and len(str(val)) >= _MIN_SECRET_LEN:
            secrets.append(str(val))
    for key in _EXTRA_SECRET_KEYS:
        val = os.environ.get(key)
        if val and len(str(val)) >= _MIN_SECRET_LEN:
            secrets.append(str(val))
    return secrets


def _scan(payload: Any) -> str | None:
    """Return the first secret-substring found in a JSON-encodable payload, or None."""
    if not payload:
        return None
    secrets = _collect_secrets()
    if not secrets:
        return None
    try:
        blob = json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return None
    for secret in secrets:
        if secret in blob:
            return secret
    return None


class A2APrivacyPlugin(BasePlugin):
    """Outbound A2A privacy guardrail. Symmetric on before+after tool."""

    def __init__(self) -> None:
        super().__init__(name="a2a_privacy")

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict | None:
        if tool.name not in _A2A_RISK_TOOLS:
            return None
        leak = _scan(tool_args)
        if leak is not None:
            logger.error("A2A_PRIVACY: secret found in args of %s", tool.name)
            return {
                "status": "error",
                "error_code": "A2A_SECRET_IN_ARGS",
                "message": (
                    f"Outbound A2A tool call `{tool.name}` was blocked because "
                    "it contains a sensitive system credential. Privacy mandate: "
                    "technical DNA only, never credentials."
                ),
            }
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> dict | None:
        if tool.name not in _A2A_RISK_TOOLS:
            return None
        leak = _scan(result)
        if leak is not None:
            logger.error("A2A_PRIVACY: secret found in output of %s", tool.name)
            return {
                "status": "error",
                "error_code": "A2A_SECRET_IN_OUTPUT",
                "message": (
                    f"Technical DNA from `{tool.name}` was blocked. A system "
                    "secret was found in the generated package. Exchange cancelled."
                ),
            }
        return None
