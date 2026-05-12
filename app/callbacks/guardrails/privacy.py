"""A2A privacy guardrail: prevent credential leaks in outbound calls/DNA.

Deterministic secret-matching guardrail for A2A-risk tools (`call_friend`,
`call_agent`, `export_dna`, `add_friend`, `web_fetch`). Loads all
non-safe-listed values from `ALLOWED_CONFIG_KEYS` plus the admin
passcode + TOTP secret, then JSON-scans both the tool arguments and
the tool response for any of those literal strings. A hit returns a
synthetic error and aborts the call (or scrubs the response if we
caught it post-tool).

The match is substring + case-sensitive. False positives are possible
for very short shared substrings — `_SAFE_KEYS` excludes a few public-
knowledge env vars to dampen that, and `len > 6` filters tiny values.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)


def a2a_privacy_guardrail(tool, args, tool_context, tool_response=None):
    """
    Deterministic secret-matching guardrail for A2A tools.
    Blocks any tool call or response that contains sensitive environment variables.
    """
    from app.app_utils.config import ALLOWED_CONFIG_KEYS

    # Get tool name
    tool_name = getattr(tool, "name", "") or (tool.__name__ if callable(tool) else "")

    _A2A_RISK_TOOLS = {
        "call_friend",
        "call_agent",
        "export_dna",
        "add_friend",
        "web_fetch",
    }
    if tool_name not in _A2A_RISK_TOOLS:
        return None

    # Safe keys that are publicly known or not sensitive enough to block DNA exports
    _SAFE_KEYS = {
        "BOT_NAME",
        "GITHUB_REPO",
        "APP_NAME",
    }

    # Load all current secrets dynamically to support future evolution
    secrets = []
    for key in ALLOWED_CONFIG_KEYS:
        if key in _SAFE_KEYS:
            continue

        val = os.environ.get(key)
        # We only match secrets that are long enough to be unique/dangerous (e.g., > 6 chars)
        if val and len(str(val)) > 6:
            secrets.append(str(val))

    # Also catch the admin passcode and TOTP secret
    for extra_key in ["ADMIN_PASSCODE", "ADMIN_TOTP_SECRET"]:
        val = os.environ.get(extra_key)
        if val and len(str(val)) > 6:
            secrets.append(str(val))

    # 1. Check Arguments (Preventing leak via query/URL)
    args_json = json.dumps(args)
    for secret in secrets:
        if secret in args_json:
            logger.error(
                "A2A PRIVACY VIOLATION: Secret detected in arguments for %s", tool_name
            )
            return {
                "status": "error",
                "message": (
                    f"Guardrail Intervention: Outbound A2A tool call `{tool_name}` was blocked "
                    f"because it contains a sensitive system credential (API Key/Token). "
                    f"Privacy mandate: Technical DNA only. Never share credentials."
                ),
            }

    # 2. Check Response (Preventing leak via DNA packaging or fetching)
    if tool_response is not None:
        resp_json = json.dumps(tool_response)
        for secret in secrets:
            if secret in resp_json:
                logger.error(
                    "A2A PRIVACY VIOLATION: Secret detected in output of %s", tool_name
                )
                return {
                    "status": "error",
                    "message": (
                        f"Guardrail Intervention: Technical DNA from `{tool_name}` was blocked. "
                        f"A system secret was found in the generated package. DNA exchange cancelled."
                    ),
                }

    return None
