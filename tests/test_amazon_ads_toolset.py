"""Amazon Ads MCP integration — allowlist, auth bridge, guardrail, toolset.

No live HTTP: the LWA token mint is always monkeypatched. conftest's
``restrict_live_http_calls`` is a second safety net.
"""

import asyncio
import logging
import types

import pytest

from app.callbacks.guardrails import amazon_ads_allowlist_guardrail
from app.toolsets.amazon_ads import (
    AMAZON_ADS_ALLOWED_TOOLS,
    AMAZON_ADS_TOOL_PREFIXES,
    AmazonAdsToolset,
)
from app.tools import amazon_ads_auth as auth


def _tool(name):
    return types.SimpleNamespace(name=name)


# ---------------------------------------------------------------------------
# Allowlist shape
# ---------------------------------------------------------------------------

class TestAllowlist:
    def test_exactly_23(self):
        assert len(AMAZON_ADS_ALLOWED_TOOLS) == 23

    def test_mutation_posture(self):
        # Reconciled with the user contract: read-heavy, NO campaign/budget/
        # target/ad mutation. Every create/update/delete name in the list is
        # one of the explicitly user-whitelisted state-changers below.
        state_changers = {
            t
            for t in AMAZON_ADS_ALLOWED_TOOLS
            if any(v in t for v in ("create", "update", "delete"))
        }
        assert state_changers == {
            # account setting (not advertiser state)
            "account_management-update_account_timezone",
            # report-artifact lifecycle (not advertiser state)
            "reporting-create_campaign_report",
            "reporting-create_inventory_report",
            "reporting-create_product_report",
            "reporting-create_report",
            "reporting-delete_report",
            # the one opted-in DSP creation
            "campaign_management-dsp_create_conversion_tracking_products",
        }

    def test_no_campaign_state_mutation_tools(self):
        # Hard contract: every campaign_management-* tool is a query or
        # eligibility check — the sole exception is the user-opted-in DSP
        # conversion-tracking creator. No campaign/budget/target writer.
        cm = {
            t for t in AMAZON_ADS_ALLOWED_TOOLS
            if t.startswith("campaign_management-")
        }
        for t in cm:
            verb = t.split("-", 1)[1]
            assert (
                verb.startswith("query_")
                or verb == "check_product_eligibility"
                or verb == "dsp_create_conversion_tracking_products"
            ), f"unexpected campaign_management tool: {t}"

    def test_every_allowed_tool_has_known_prefix(self):
        for t in AMAZON_ADS_ALLOWED_TOOLS:
            assert t.startswith(AMAZON_ADS_TOOL_PREFIXES), t


# ---------------------------------------------------------------------------
# Defensive guardrail
# ---------------------------------------------------------------------------

class TestGuardrail:
    def test_allows_whitelisted(self):
        assert (
            amazon_ads_allowlist_guardrail(
                _tool("reporting-create_report"), {}, object()
            )
            is None
        )

    def test_allows_the_one_write(self):
        assert (
            amazon_ads_allowlist_guardrail(
                _tool("account_management-update_account_timezone"), {}, object()
            )
            is None
        )

    def test_blocks_non_whitelisted_ads_tool(self):
        res = amazon_ads_allowlist_guardrail(
            _tool("campaign_management-create_campaign"), {}, object()
        )
        assert res is not None
        assert res["status"] == "error"
        assert "create_campaign" in res["message"]

    def test_blocks_disabled_domain(self):
        # AMC tools are entirely off even though the skill is installed.
        res = amazon_ads_allowlist_guardrail(
            _tool("amazon_marketing_cloud-create_workflow"), {}, object()
        )
        assert res is not None and res["status"] == "error"

    def test_ignores_non_ads_tool(self):
        assert (
            amazon_ads_allowlist_guardrail(_tool("scratchpad_write"), {}, object())
            is None
        )
        assert (
            amazon_ads_allowlist_guardrail(_tool("transfer_to_agent"), {}, object())
            is None
        )

    def test_handles_client_prefixed_names(self):
        # Allowed even if wrapped by a client-side prefix.
        assert (
            amazon_ads_allowlist_guardrail(
                _tool("mcp__amazon-ads__reporting-create_report"), {}, object()
            )
            is None
        )
        # Blocked even if wrapped.
        res = amazon_ads_allowlist_guardrail(
            _tool("mcp__amazon-ads__campaign_management-create_campaign"),
            {},
            object(),
        )
        assert res is not None and res["status"] == "error"

    def test_none_tool_is_noop(self):
        assert amazon_ads_allowlist_guardrail(None, {}, object()) is None
        assert (
            amazon_ads_allowlist_guardrail(_tool(None), {}, object()) is None
        )


# ---------------------------------------------------------------------------
# Auth bridge — header_provider / token cache / region
# ---------------------------------------------------------------------------

class TestAuthBridge:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        # Each test starts with a cold token cache.
        auth._token_cache.invalidate()
        yield
        auth._token_cache.invalidate()

    def _vault(self, monkeypatch, mapping):
        monkeypatch.setattr(
            auth.vault, "get", lambda k, d="": mapping.get(k, d)
        )

    def test_header_provider_dynamic_context(self, monkeypatch):
        self._vault(
            monkeypatch,
            {
                "ADS_API_CLIENT_ID": "cid",
                "ADS_API_CLIENT_SECRET": "sec",
                "ADS_API_REFRESH_TOKEN": "rt",
            },
        )
        monkeypatch.setattr(auth, "_mint_access_token", lambda: ("AT", 3600))
        auth._token_cache.ensure_warm()  # off-loop mint, as get_tools does

        h = auth.header_provider(None)
        assert h["Amazon-Ads-ClientId"] == "cid"
        assert h["Authorization"] == "Bearer AT"
        assert h["Accept"] == "application/json, text/event-stream"
        # Dynamic = no FIXED selection headers.
        assert "Amazon-Ads-AI-Account-Selection-Mode" not in h

    def test_header_provider_fixed_context(self, monkeypatch):
        self._vault(
            monkeypatch,
            {
                "ADS_API_CLIENT_ID": "cid",
                "ADS_API_CLIENT_SECRET": "sec",
                "ADS_API_REFRESH_TOKEN": "rt",
                "ADS_API_ACCOUNT_SELECTION_MODE": "FIXED",
                "ADS_API_DEFAULT_PROFILE_ID": "1866085214308169",
            },
        )
        monkeypatch.setattr(auth, "_mint_access_token", lambda: ("AT", 3600))
        auth._token_cache.ensure_warm()

        h = auth.header_provider(None)
        assert h["Amazon-Ads-AI-Account-Selection-Mode"] == "FIXED"
        assert h["Amazon-Advertising-API-Scope"] == "1866085214308169"

    def test_fixed_mode_without_identifier_raises(self, monkeypatch):
        self._vault(
            monkeypatch,
            {
                "ADS_API_CLIENT_ID": "cid",
                "ADS_API_CLIENT_SECRET": "sec",
                "ADS_API_REFRESH_TOKEN": "rt",
                "ADS_API_ACCOUNT_SELECTION_MODE": "FIXED",
            },
        )
        monkeypatch.setattr(auth, "_mint_access_token", lambda: ("AT", 3600))
        auth._token_cache.ensure_warm()
        with pytest.raises(auth.AmazonAdsAuthError):
            auth.header_provider(None)

    def test_missing_credentials_raises_loudly(self, monkeypatch, caplog):
        self._vault(monkeypatch, {})  # nothing configured
        with caplog.at_level(logging.ERROR):
            with pytest.raises(auth.AmazonAdsAuthError):
                auth.header_provider(None)
        assert any("misconfigured" in r.message for r in caplog.records)

    def test_credentials_present(self, monkeypatch):
        self._vault(monkeypatch, {"ADS_API_CLIENT_ID": "x"})
        assert auth.credentials_present() is False
        self._vault(
            monkeypatch,
            {
                "ADS_API_CLIENT_ID": "x",
                "ADS_API_CLIENT_SECRET": "y",
                "ADS_API_REFRESH_TOKEN": "z",
            },
        )
        assert auth.credentials_present() is True

    def test_token_cache_reuses_until_expiry(self, monkeypatch):
        calls = {"n": 0}

        def _mint():
            calls["n"] += 1
            return (f"AT{calls['n']}", 3600)

        monkeypatch.setattr(auth, "_mint_access_token", _mint)
        auth._token_cache.ensure_warm()
        auth._token_cache.ensure_warm()  # warm — no second mint
        assert auth._token_cache.get() == "AT1"
        assert calls["n"] == 1

    def test_get_is_pure_no_network_when_cold(self, monkeypatch):
        # Reviewer fix #3: get() MUST NOT mint (it runs on the event loop).
        def _boom():
            raise AssertionError("get() must not call _mint_access_token")

        monkeypatch.setattr(auth, "_mint_access_token", _boom)
        with pytest.raises(auth.AmazonAdsAuthError):
            auth._token_cache.get()

    def test_ensure_warm_starts_daemon_refresher(self, monkeypatch):
        monkeypatch.setattr(auth, "_mint_access_token", lambda: ("AT", 3600))
        auth._token_cache.ensure_warm()
        t = auth._token_cache._refresher
        assert t is not None and t.is_alive() and t.daemon

    def test_invalidate_then_ensure_warm_single_refresher(self, monkeypatch):
        # Reviewer fix #2: an invalidate -> ensure_warm cycle must leave
        # EXACTLY one live refresher and stop the previous cohort.
        import threading

        monkeypatch.setattr(auth, "_mint_access_token", lambda: ("AT", 3600))
        auth._token_cache.ensure_warm()
        t1 = auth._token_cache._refresher
        assert t1 is not None and t1.is_alive()

        auth._token_cache.invalidate()
        t1.join(timeout=3)
        assert not t1.is_alive(), "old refresher cohort did not stop"

        auth._token_cache.ensure_warm()
        t2 = auth._token_cache._refresher
        assert t2 is not None and t2 is not t1 and t2.is_alive()

        alive = [
            th
            for th in threading.enumerate()
            if th.name.startswith("amazon-ads-token-refresher")
            and th.is_alive()
        ]
        assert len(alive) == 1, f"refresher leak: {[t.name for t in alive]}"

    def test_token_generation_changes_only_on_rotation(self, monkeypatch):
        seq = iter([("A", 3600), ("A", 3600), ("B", 3600)])
        monkeypatch.setattr(auth, "_mint_access_token", lambda: next(seq))
        g0 = auth._token_cache.token_generation
        auth._token_cache._store(*("A", 3600))
        g1 = auth._token_cache.token_generation
        auth._token_cache._store(*("A", 3600))  # same value — no bump
        g2 = auth._token_cache.token_generation
        auth._token_cache._store(*("B", 3600))  # rotated — bump
        g3 = auth._token_cache.token_generation
        assert g1 == g0 + 1 and g2 == g1 and g3 == g2 + 1

    def test_region_resolution(self, monkeypatch):
        self._vault(monkeypatch, {"ADS_API_REGION": "EU"})
        assert auth.mcp_url() == "https://advertising-ai-eu.amazon.com/mcp"
        self._vault(monkeypatch, {"ADS_API_REGION": "bogus"})
        assert auth.mcp_url() == "https://advertising-ai.amazon.com/mcp"  # NA
        self._vault(monkeypatch, {})
        assert auth.mcp_url() == "https://advertising-ai.amazon.com/mcp"


# ---------------------------------------------------------------------------
# Toolset wiring
# ---------------------------------------------------------------------------

class TestToolset:
    def test_basetoolset_contract_attrs_present(self):
        # Regression: __init__ MUST call super() — ADK reads tool_name_prefix
        # / tool_filter via get_tools_with_prefix at agent load. Missing =
        # AttributeError on the live bot ('no attribute tool_name_prefix').
        ts = AmazonAdsToolset()
        assert ts.tool_name_prefix is None
        assert ts.tool_filter is None

    def test_get_tools_with_prefix_does_not_raise(self, monkeypatch):
        # Exercise the exact ADK code path that crashed in production.
        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: False
        )
        ts = AmazonAdsToolset()
        tools = asyncio.run(ts.get_tools_with_prefix())
        assert tools == []

    def test_degrades_loudly_without_credentials(self, monkeypatch, caplog):
        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: False
        )
        ts = AmazonAdsToolset()
        with caplog.at_level(logging.CRITICAL):
            tools = asyncio.run(ts.get_tools())
        assert tools == []
        assert any(
            "Amazon Ads MCP DISABLED" in r.message for r in caplog.records
        )

    def test_empty_after_filter_degrades_loudly(self, monkeypatch, caplog):
        # Reviewer fix #1: connection OK but allowlist matched ZERO server
        # tools ⇒ CRITICAL + empty, never silent.
        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: True
        )
        monkeypatch.setattr(auth._token_cache, "ensure_warm", lambda: None)
        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.header_provider", lambda ctx=None: {}
        )

        class _FakeInner:
            async def get_tools(self, ctx=None):
                return []

            async def close(self):
                return None

        ts = AmazonAdsToolset()
        monkeypatch.setattr(ts, "_build_inner", lambda *a, **k: _FakeInner())
        with caplog.at_level(logging.CRITICAL):
            tools = asyncio.run(ts.get_tools())
        assert tools == []
        assert any("matched ZERO" in r.message for r in caplog.records)
        assert ts._degraded is True

    def test_auth_failure_degrades_loudly(self, monkeypatch, caplog):
        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: True
        )

        def _warm_boom():
            raise auth.AmazonAdsAuthError("refresh token rotated")

        monkeypatch.setattr(auth._token_cache, "ensure_warm", _warm_boom)

        class _FakeInner:
            async def get_tools(self, ctx=None):
                raise AssertionError("should not reach listing")

            async def close(self):
                return None

        ts = AmazonAdsToolset()
        monkeypatch.setattr(ts, "_build_inner", lambda *a, **k: _FakeInner())
        with caplog.at_level(logging.CRITICAL):
            tools = asyncio.run(ts.get_tools())
        assert tools == []
        assert any(
            "auth or tool listing failed" in r.message for r in caplog.records
        )

    def test_inner_reused_until_token_rotates(self, monkeypatch):
        # Reviewer fix #1: steady-state turns must NOT rebuild / re-handshake.
        # Rebuild only when token generation changes.
        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: True
        )
        monkeypatch.setattr(auth._token_cache, "ensure_warm", lambda: None)
        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.header_provider", lambda ctx=None: {}
        )
        # Pin a stable token generation we control.
        monkeypatch.setattr(auth._token_cache, "_token_generation", 7)

        builds = {"n": 0}
        closes = {"n": 0}

        class _Tool:
            name = "reporting-create_report"

        class _FakeInner:
            async def get_tools(self, ctx=None):
                return [_Tool()]

            async def close(self):
                closes["n"] += 1

        def _factory(*a, **k):
            builds["n"] += 1
            return _FakeInner()

        ts = AmazonAdsToolset()
        monkeypatch.setattr(ts, "_build_inner", _factory)

        first = asyncio.run(ts.get_tools())
        inner_after_1 = ts._inner
        asyncio.run(ts.get_tools())  # same generation — reuse
        assert builds["n"] == 1, "rebuilt despite unchanged token generation"
        assert ts._inner is inner_after_1
        assert closes["n"] == 0
        assert len(first) == 1

        # Rotate the token: generation bumps -> exactly one rebuild + close.
        monkeypatch.setattr(auth._token_cache, "_token_generation", 8)
        asyncio.run(ts.get_tools())
        assert builds["n"] == 2
        assert closes["n"] == 1
        assert ts._built_token_gen == 8

    def test_build_inner_uses_native_mcptoolset(self, monkeypatch):
        # Credentials present but we never open a session — just verify the
        # inner toolset is ADK-native McpToolset wired to the right URL +
        # filter, without any network.
        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: True
        )
        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.mcp_url",
            lambda: "https://advertising-ai.amazon.com/mcp",
        )
        from google.adk.tools.mcp_tool import McpToolset

        ts = AmazonAdsToolset()
        inner = ts._build_inner({"Authorization": "Bearer x"})
        assert isinstance(inner, McpToolset)
        # Auth must ride on connection_params.headers (the no-context
        # tools/list path) — regression-lock the ADK header_provider gap fix.
        assert inner._connection_params.headers == {"Authorization": "Bearer x"}


class TestAgentWiring:
    """Reviewer fix #4: regression-lock the actual agent wiring so a future
    edit can't silently unmount the toolset / guardrail or leak Ads tools
    onto the analyst."""

    def test_amazon_agent_mounts_ads_toolset_and_guardrail(self):
        from app.sub_agents.amazon_agent import amazon_agent

        assert any(
            isinstance(t, AmazonAdsToolset) for t in amazon_agent.tools
        ), "AmazonAdsToolset not mounted on AmazonAgent"

        names = [
            getattr(c, "__name__", "")
            for c in (amazon_agent.before_tool_callback or [])
        ]
        assert "amazon_ads_allowlist_guardrail" in names

    def test_amazon_agent_loads_ads_skills(self):
        from google.adk.tools import skill_toolset

        from app.sub_agents.amazon_agent import amazon_agent

        skill_names: set[str] = set()
        for t in amazon_agent.tools:
            if isinstance(t, skill_toolset.SkillToolset):
                # ADK stores loaded skills as a {name: Skill} dict.
                skill_names |= set(t._skills.keys())
        assert {"ads-reporting", "account-context-identifiers"} <= skill_names

    def test_data_analyst_has_no_ads_access(self):
        # The handoff contract depends on DataAnalyst having ZERO Ads tools.
        from app.sub_agents.amazon_data_analyst_agent import (
            amazon_data_analyst_agent as da,
        )

        assert not any(
            isinstance(t, AmazonAdsToolset) for t in da.tools
        )
        names = [
            getattr(c, "__name__", "")
            for c in (da.before_tool_callback or [])
        ]
        assert "amazon_ads_allowlist_guardrail" not in names

    def test_head_agent_routes_ads(self):
        from app.sub_agents.amazon_head_agent import amazon_head_agent

        instr = amazon_head_agent.instruction
        assert "Amazon Ads MCP" in instr
        assert "scratchpad_read(name, owner='AmazonAgent')" in instr
        sub = {s.name for s in amazon_head_agent.sub_agents}
        assert {"AmazonAgent", "AmazonDataAnalystAgent"} <= sub


def test_adk_mcp_imports_available():
    """The integration depends on these ADK symbols existing in the pinned
    version — fail loudly here if an ADK bump moves them."""
    from google.adk.tools.mcp_tool import McpToolset  # noqa: F401
    from google.adk.tools.mcp_tool.mcp_session_manager import (  # noqa: F401
        StreamableHTTPConnectionParams,
    )
