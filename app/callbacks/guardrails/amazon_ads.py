"""Amazon Ads allowlist guardrail — defence-in-depth `before_tool` gate.

``McpToolset(tool_filter=...)`` already restricts what Amazon's remote Ads
MCP advertises to the model. This guardrail is the *second* line: even if
the upstream filter drifts (ADK regression, server adds a tool inside an
allowed package, prefix change), no Amazon-Ads-namespace tool outside
``AMAZON_ADS_ALLOWED_TOOLS`` can execute.

Posture is read-heavy: NO campaign/budget/ad/target mutation is allowed.
The only state-changing tools on the allowlist — all user-whitelisted —
are ``account_management-update_account_timezone`` (account setting),
``reporting-create_*`` / ``reporting-delete_report`` (report-artifact
lifecycle, not advertiser state), and
``campaign_management-dsp_create_conversion_tracking_products`` (the one
opted-in DSP creation). Everything else — campaign/budget create/update/
delete, Amazon Live, AMC, billing, invitations — is blocked here
regardless of what the MCP server offers.

Scope: only tools in a known Amazon Ads package namespace are judged. Any
other tool (Keepa, SP-API, scratchpad, transfer_to_agent, …) is ignored
(returns ``None``) so this composes cleanly with other ``before_tool``
callbacks in list order (``docs/AI_EDITS.md`` §5).

Rule 13: a block logs at ``error`` and returns ``{"status": "error",
"message": ...}`` which the agent surfaces verbatim.
"""

import logging

from app.toolsets.amazon_ads import (
    AMAZON_ADS_ALLOWED_TOOLS,
    AMAZON_ADS_TOOL_PREFIXES,
)

logger = logging.getLogger(__name__)


def _candidate_names(raw: str) -> tuple[str, ...]:
    """Names to test: the tool name and its trailing segment after any
    client-side wrapper prefix (e.g. ``mcp__amazon-ads__<name>``)."""
    names = [raw]
    if "__" in raw:
        names.append(raw.rsplit("__", 1)[-1])
    return tuple(names)


def amazon_ads_allowlist_guardrail(tool, args, tool_context, **kwargs) -> dict | None:
    """Block any Amazon-Ads-namespace tool not on the approved allowlist."""
    if not tool or not getattr(tool, "name", None):
        return None

    candidates = _candidate_names(tool.name)

    # Explicitly approved — allow.
    if any(c in AMAZON_ADS_ALLOWED_TOOLS for c in candidates):
        return None

    # Is this an Amazon Ads tool at all? If not, not our concern.
    is_ads_tool = any(
        c.startswith(AMAZON_ADS_TOOL_PREFIXES) for c in candidates
    )
    if not is_ads_tool:
        return None

    logger.error(
        "Amazon Ads guardrail BLOCKED non-allowlisted tool %r "
        "(not in the user-approved 23-tool allowlist).",
        tool.name,
    )
    return {
        "status": "error",
        "message": (
            f"Guardrail Intervention: `{tool.name}` is an Amazon Ads tool "
            f"that is NOT on the approved allowlist. AmazonAgent is "
            f"restricted to 23 read-heavy Amazon Ads tools (no campaign/"
            f"budget/target mutation). This tool is blocked. If you need "
            f"it, the operator must explicitly add it to "
            f"`AMAZON_ADS_ALLOWED_TOOLS`."
        ),
    }
