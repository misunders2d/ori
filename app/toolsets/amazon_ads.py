"""Amazon Ads MCP toolset — ADK-native wrapper over Amazon's remote MCP.

Connects ``AmazonAgent`` to Amazon's **hosted** Ads MCP server (Streamable
HTTP) via ADK's stock ``McpToolset``. Two guardrails, defence in depth:

1. ``McpToolset(tool_filter=...)`` — only the user-approved 23 tools are ever
   advertised to the model (``AMAZON_ADS_ALLOWED_TOOLS``).
2. ``amazon_ads_allowlist_guardrail`` (``before_tool``) — hard-blocks any
   Amazon-Ads-namespace tool not on the allowlist even if filtering drifts.

Mutation posture (reconciled with the user contract — see
``project-amazon-ads-allowed-tools`` memory + the user's PDF selection
2026-05-15). The list is read-heavy: **no campaign / budget / ad / target /
ad-group mutation is permitted**. The only state-changing tools, all
explicitly user-whitelisted, are:

* ``account_management-update_account_timezone`` — account-setting write.
* ``reporting-create_*`` / ``reporting-delete_report`` — async report-artifact
  lifecycle (requests a report / removes a report; does **not** mutate
  advertiser state).
* ``campaign_management-dsp_create_conversion_tracking_products`` — the one
  DSP creation tool the user opted into.

Everything else (campaign/budget/target writes, Amazon Live, AMC, billing,
invitations) is excluded. This module is the single source of truth for the
list — the guardrail and tests import it from here, never re-declare it
(``docs/AI_EDITS.md`` §1/§2).

Credentials missing ⇒ the toolset degrades to an empty tool list **and**
logs ``critical`` (Rule 13 — a silently disengaged integration is forbidden;
the degradation must be observable).
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.tools.base_toolset import BaseToolset

logger = logging.getLogger(__name__)

# --- User-approved allowlist (23/96). DO NOT widen without re-confirmation. ---
# Names are the Amazon Ads MCP server-side tool names (no client prefix).
AMAZON_ADS_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        # Account Management (2/7) — query is read; timezone is an
        # account-setting write (no advertiser-state mutation).
        "account_management-query_advertiser_account",
        "account_management-update_account_timezone",
        # Ads Accounts (2/3)
        "ads_accounts-get_ads_account",
        "ads_accounts-list_ads_accounts",
        # Campaign Management (7/28) — queries + eligibility, NO campaign/
        # budget/target writes. dsp_create_conversion_tracking_products is the
        # one DSP creation the user explicitly opted into.
        "campaign_management-check_product_eligibility",
        "campaign_management-dsp_create_conversion_tracking_products",
        "campaign_management-query_ad",
        "campaign_management-query_ad_association",
        "campaign_management-query_ad_group",
        "campaign_management-query_campaign",
        "campaign_management-query_target",
        # Eligibility (2/2)
        "eligibility-product_list",
        "eligibility-programs",
        # Manager Accounts (1/4)
        "manager_accounts-get_manager_accounts",
        # Reporting (6/6) — async report-artifact lifecycle; create/delete
        # request/remove a REPORT, they do not mutate advertiser state.
        "reporting-create_campaign_report",
        "reporting-create_inventory_report",
        "reporting-create_product_report",
        "reporting-create_report",
        "reporting-delete_report",
        "reporting-retrieve_report",
        # User Permissions (1/3)
        "user_permissions-list_user_permissions",
        # User Roles (1/1)
        "user_roles-list_user_roles",
        # Users (1/1)
        "users-list_users",
    }
)

# Every Amazon Ads MCP package namespace (allowed + explicitly-disabled
# domains). Used by the defensive guardrail to recognise "is this an Amazon
# Ads tool at all" so a blocked write (e.g. campaign_management-create_campaign)
# is caught rather than waved through as a foreign tool.
AMAZON_ADS_TOOL_PREFIXES: tuple[str, ...] = (
    "account_management-",
    "ads_accounts-",
    "campaign_management-",
    "eligibility-",
    "manager_accounts-",
    "reporting-",
    "user_permissions-",
    "user_roles-",
    "users-",
    "advertiser_product_group_eligibility-",
    "amazon_live-",
    "amazon_marketing_cloud-",
    "amc-",
    "billing-",
    "terms_token-",
    "test_accounts-",
    "user_invitation-",
    "user_invitations-",
)

# Connection timeouts (seconds). Reports can stream for a while; the read
# timeout is generous, the connect timeout is not.
_CONNECT_TIMEOUT = 15.0
_SSE_READ_TIMEOUT = 300.0


class AmazonAdsToolset(BaseToolset):
    """Amazon Ads — remote MCP (Streamable HTTP), filtered to 23 tools.

    Lazily constructs the inner ``McpToolset`` on first ``get_tools`` so that
    importing the agent module performs no network I/O and needs no vault.
    """

    def __init__(self) -> None:
        # ADK BaseToolset.__init__ sets self.tool_filter / self.tool_name_prefix.
        # ADK assembles agent tools via get_tools_with_prefix(), which reads
        # self.tool_name_prefix — skipping super() raises AttributeError at
        # agent load (the only repo toolset that overrides __init__).
        super().__init__()
        self._inner: Any = None
        self._degraded = False
        # Token generation the cached _inner was built with. Rebuild only
        # when None or this no longer matches (i.e. the token rotated) —
        # steady-state turns reuse the same MCP session (reviewer fix #1:
        # no per-turn handshake churn / repeated 403 close noise).
        self._built_token_gen: int | None = None
        self._logged_wiring = False

    def _build_inner(self, base_headers: dict[str, str]) -> Any:
        # Imported lazily — keeps ADK MCP optionality out of import time and
        # mirrors how other toolsets defer heavy imports into get_tools.
        from google.adk.tools.mcp_tool import McpToolset
        from google.adk.tools.mcp_tool.mcp_session_manager import (
            StreamableHTTPConnectionParams,
        )

        from app.tools.amazon_ads_auth import header_provider, mcp_url

        url = mcp_url()
        # First wiring at info; subsequent rebuilds (token rotation only) at
        # debug — keeps the log quiet in steady state.
        if not self._logged_wiring:
            logger.info("Amazon Ads MCP: wiring remote endpoint %s", url)
            self._logged_wiring = True
        else:
            logger.debug("Amazon Ads MCP: rebuilding session for %s", url)
        # ADK applies `header_provider` ONLY when a ReadonlyContext is present
        # (mcp_toolset.py: `if self._header_provider and readonly_context`).
        # The `tools/list` we issue at assembly has no context, so auth must
        # also ride on connection_params.headers (ADK's `_merge_headers`
        # always seeds from these). header_provider still overlays a freshly
        # cached token on every real tool call (context present). Inner is
        # rebuilt only when the token rotates so this base snapshot can't go
        # stale while avoiding per-turn session churn.
        return McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=url,
                headers=dict(base_headers),
                timeout=_CONNECT_TIMEOUT,
                sse_read_timeout=_SSE_READ_TIMEOUT,
            ),
            tool_filter=sorted(AMAZON_ADS_ALLOWED_TOOLS),
            header_provider=header_provider,
        )

    def _function_tools(self) -> list:
        # Deterministic helper(s) — always present, even when the MCP path
        # degrades, because the helper self-guards credentials and returns a
        # clean Rule-13 error. Keeps the common "<account>, <marketplace>,
        # yesterday's performance" ask off the LLM's hand-written payloads.
        from google.adk.tools.function_tool import FunctionTool

        from app.tools.amazon_ads_reports import amazon_ads_performance_report

        return [FunctionTool(func=amazon_ads_performance_report)]

    async def get_tools(self, readonly_context=None):
        from app.tools.amazon_ads_auth import credentials_present

        fts = self._function_tools()

        if not credentials_present():
            # Rule 13: do not disengage silently. Empty toolset is acceptable
            # degraded behaviour ONLY with a CRITICAL log at the moment it
            # engages.
            logger.critical(
                "Amazon Ads MCP DISABLED — LWA credentials absent from vault "
                "(need ADS_API_CLIENT_ID / ADS_API_CLIENT_SECRET / "
                "ADS_API_REFRESH_TOKEN). AmazonAgent will have NO Amazon Ads "
                "tools. Run `uv run python scripts/ads_oauth_helper.py`."
            )
            self._degraded = True
            return fts

        try:
            # Reviewer fix #3: mint the LWA token OFF the event loop so the
            # blocking HTTP exchange never stalls it. After this returns the
            # token is warm + a daemon keeps it warm, so header_provider
            # (called on-loop during session setup) only reads memory.
            import asyncio

            from app.tools.amazon_ads_auth import _token_cache, header_provider

            await asyncio.to_thread(_token_cache.ensure_warm)

            # Rebuild ONLY when there is no session yet or the token actually
            # rotated (generation changed). Steady-state turns reuse the same
            # McpToolset — no re-handshake, no repeated 403 close noise.
            current_gen = _token_cache.token_generation
            if self._inner is None or self._built_token_gen != current_gen:
                prev = self._inner
                self._inner = self._build_inner(
                    header_provider(readonly_context)
                )
                self._built_token_gen = current_gen
                if prev is not None:
                    try:
                        await prev.close()
                    except Exception as exc:  # noqa: BLE001
                        logger.error(
                            "Amazon Ads MCP: closing rotated session "
                            "failed: %r",
                            exc,
                        )

            tools = await self._inner.get_tools(readonly_context)
        except Exception as exc:  # noqa: BLE001 — must not break agent assembly
            # Rule 13: reachable MCP / auth failure logged CRITICAL; agent
            # still boots without Ads tools rather than failing the whole tree.
            logger.critical(
                "Amazon Ads MCP unreachable / auth or tool listing failed: "
                "%r. AmazonAgent will have NO Amazon Ads tools this boot.",
                exc,
            )
            self._degraded = True
            return fts

        if not tools:
            # Reviewer fix #1: connection succeeded but the allowlist matched
            # ZERO server tools — almost certainly the server renamed/repackaged
            # tools and our filter (and the guardrail) are now stale. This is
            # NOT acceptable silent degradation: log CRITICAL and surface it.
            logger.critical(
                "Amazon Ads MCP returned a connection but the 23-tool "
                "allowlist matched ZERO server tools. The remote tool "
                "namespace likely changed — AMAZON_ADS_ALLOWED_TOOLS / the "
                "guardrail prefixes are stale and MUST be reconciled against "
                "the live `tools/list`. AmazonAgent has NO Amazon Ads tools."
            )
            self._degraded = True
            return fts

        self._degraded = False
        return fts + tools

    async def close(self) -> None:
        # NOTE: Amazon's remote MCP returns HTTP 403 to the streamable-http
        # session DELETE that ADK issues on teardown ("Session termination
        # failed: 403" logged by ADK). It is benign — the preceding
        # tools/list and tool calls succeed; only the explicit terminate is
        # refused server-side. Verified live 2026-05-17 (23/23 tools listed
        # OK). We swallow + log here so it never propagates.
        if self._inner is not None:
            try:
                await self._inner.close()
            except Exception as exc:  # noqa: BLE001
                logger.error("Amazon Ads MCP toolset close failed: %r", exc)
