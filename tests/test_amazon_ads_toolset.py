"""Amazon Ads MCP integration — allowlist, auth bridge, guardrail, toolset.

No live HTTP: the LWA token mint is always monkeypatched. conftest's
``restrict_live_http_calls`` is a second safety net.
"""

import asyncio
import logging
import os
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

class TestReportsHelper:
    """Deterministic reporting layer — pure logic, no network."""

    def test_period_resolver_relative(self):
        from app.tools.amazon_ads_reports import _resolve_period

        for p in ("yesterday", "today", "last_7_days", "last_30_days",
                  "this_month", "last_month"):
            r = _resolve_period(p, "", "", "America/Los_Angeles")
            assert isinstance(r, tuple), p
            s, e = r
            assert s <= e

    def test_period_custom_and_errors(self):
        from app.tools.amazon_ads_reports import _resolve_period

        assert _resolve_period("custom", "2026-05-01", "2026-05-10",
                               "America/Los_Angeles") == (
            "2026-05-01", "2026-05-10")
        assert isinstance(
            _resolve_period("custom", "", "", "America/Los_Angeles"), str)
        assert isinstance(
            _resolve_period("custom", "bad", "x", "UTC"), str)
        assert isinstance(
            _resolve_period("nope", "", "", "America/Los_Angeles"), str)
        assert isinstance(
            _resolve_period("yesterday", "", "", "Mars/Phobos"), str)

    def test_build_body_families(self):
        from app.tools.amazon_ads_reports import _build_body

        camp = _build_body("campaign", "g.x", "2026-05-17", "2026-05-17",
                           "US", None)
        assert camp["reports"][0]["query"]["filter"]["on"]["values"] == ["US"]
        assert "budgetCurrency.value" in camp["reports"][0]["query"]["fields"]

        prod = _build_body("product", "g.x", "2026-05-17", "2026-05-17",
                           "US", None)
        assert "query" not in prod["reports"][0]
        assert "periods" in prod["reports"][0]

        cust = _build_body("custom", "g.x", "2026-05-17", "2026-05-17",
                           "", ["dateRange.value", "metric.clicks"])
        assert cust["reports"][0]["query"]["fields"] == [
            "dateRange.value", "metric.clicks"]
        assert "filter" not in cust["reports"][0]["query"]  # no marketplace

        assert isinstance(
            _build_body("bogus", "g.x", "a", "b", "US", None), str)

    def test_tool_for_family(self):
        from app.tools.amazon_ads_reports import _tool_for_family

        assert _tool_for_family("campaign") == "reporting-create_report"
        assert _tool_for_family("product") == (
            "reporting-create_product_report")
        assert _tool_for_family("inventory") == (
            "reporting-create_inventory_report")

    def test_extract_accounts_pinned_path(self):
        from app.tools.amazon_ads_reports import _extract_accounts

        # Pinned live shape advertiserAccounts[].{advertiserAccountId,
        # displayName}. Fake placeholder name only (F2 — no real account).
        out = _extract_accounts(
            {"advertiserAccounts": [
                {"advertiserAccountId": "amzn1.ads-account.g.fake",
                 "displayName": "Acme Test Account",
                 "isGlobalAccount": True}]})
        assert out == [("Acme Test Account", "amzn1.ads-account.g.fake")]

    def test_extract_accounts_fails_loud_on_bad_shape(self):
        from app.tools.amazon_ads_reports import (
            _UnrecognizedShape,
            _extract_accounts,
        )

        with pytest.raises(_UnrecognizedShape):
            _extract_accounts({"unexpected": []})

    def test_unwrap_envelope(self):
        from app.tools.amazon_ads_reports import (
            _ApiError,
            _UnrecognizedShape,
            _unwrap_envelope,
        )

        # Verified live shape: {"error": None, "success": [...]}.
        assert _unwrap_envelope(
            {"error": None, "success": [1, 2]}, "x") == [1, 2]
        # Non-enveloped passes through (e.g. query_advertiser_account).
        assert _unwrap_envelope(
            {"advertiserAccounts": []}, "x") == {"advertiserAccounts": []}
        # Amazon-side error → _ApiError, ids masked.
        with pytest.raises(_ApiError) as ei:
            _unwrap_envelope(
                {"error": "bad acct amzn1.ads-account.g.deadbeef0000",
                 "success": None}, "create_report")
        assert "amzn1" not in str(ei.value) and "<id>" in str(ei.value)
        with pytest.raises(_UnrecognizedShape):
            _unwrap_envelope({"error": None, "success": None}, "x")

    def test_extract_report_ids_envelope(self):
        from app.tools.amazon_ads_reports import (
            _ApiError,
            _UnrecognizedShape,
            _extract_report_ids,
        )

        env = {"error": None,
               "success": [{"index": 0,
                            "report": {"reportId": "r1",
                                       "status": "PENDING"}}]}
        assert _extract_report_ids(env) == ["r1"]
        with pytest.raises(_ApiError):
            _extract_report_ids({"error": "nope", "success": None})
        with pytest.raises(_UnrecognizedShape):
            _extract_report_ids({"error": None, "success": [{"x": 1}]})

    def test_extract_status_envelope_and_failure(self):
        from app.tools.amazon_ads_reports import (
            _UnrecognizedShape,
            _extract_status,
        )

        ok = {"error": None,
              "success": [{"index": 0,
                           "report": {"status": "completed"}}]}
        assert _extract_status(ok) == ("COMPLETED", None)
        failed = {"error": None,
                  "success": [{"index": 0,
                               "report": {"status": "FAILED",
                                          "failureReason": "bad fields"}}]}
        assert _extract_status(failed) == ("FAILED", "bad fields")
        with pytest.raises(_UnrecognizedShape):
            _extract_status({"error": None, "success": [{"report": {}}]})

    def test_extract_result_url_from_completed_parts(self):
        from app.tools.amazon_ads_reports import _extract_result_url

        env = {"error": None, "success": [{"index": 0, "report": {
            "status": "COMPLETED",
            "completedReportParts": [
                {"url": "https://example.test/part1.csv.gz"}]}}]}
        assert _extract_result_url(env) == (
            "https://example.test/part1.csv.gz")
        # Amazon error envelope → no url, never raises here.
        assert _extract_result_url(
            {"error": "x", "success": None}) is None

    def test_csv_parser_flags_unresolved_metrics(self):
        from app.tools.amazon_ads_reports import _parse_report_csv

        good = b"metric.clicks,metric.sales\n3,10\n2,5\n"
        rows, totals, cols, matched = _parse_report_csv(good)
        assert matched and rows == 2 and totals["clicks"] == 5.0
        bad = b"foo,bar\n1,2\n"
        rows, totals, cols, matched = _parse_report_csv(bad)
        assert matched is False and totals == {}

    def test_safe_call_fails_closed(self):
        # Helper is a raw MCP path (bypasses tool_filter + guardrail) — it
        # MUST self-enforce: only approved, only read-only.
        from app.tools.amazon_ads_reports import _HelperToolGuard, _safe_call

        class _Sess:
            def __init__(self):
                self.called = []

            async def call_tool(self, name, args):
                self.called.append(name)
                return "ok"

        sess = _Sess()
        # Approved read tool → forwarded.
        assert asyncio.run(
            _safe_call(sess, "reporting-create_report", {}, 5)
        ) == "ok"
        # State-changing (on allowlist but denied for the helper).
        for bad in (
            "reporting-delete_report",
            "account_management-update_account_timezone",
            "campaign_management-dsp_create_conversion_tracking_products",
        ):
            with pytest.raises(_HelperToolGuard):
                asyncio.run(_safe_call(sess, bad, {}, 5))
        # Off-allowlist entirely.
        with pytest.raises(_HelperToolGuard):
            asyncio.run(
                _safe_call(sess, "campaign_management-create_campaign", {}, 5)
            )
        assert sess.called == ["reporting-create_report"]

    def test_helper_guards_missing_credentials(self, monkeypatch):
        from app.tools import amazon_ads_reports as rep

        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: False)
        out = asyncio.run(rep.amazon_ads_performance_report())
        assert out["status"] == "error" and "not configured" in out["message"]

    def test_helper_rejects_unknown_family(self, monkeypatch):
        from app.tools import amazon_ads_reports as rep

        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: True)
        out = asyncio.run(
            rep.amazon_ads_performance_report(report="frobnicate"))
        assert out["status"] == "error" and "report family" in out["message"]

    def test_helper_custom_period_needs_dates(self, monkeypatch):
        from app.tools import amazon_ads_reports as rep

        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: True)
        out = asyncio.run(
            rep.amazon_ads_performance_report(
                account="acme", period="custom"))
        assert out["status"] == "error" and "custom" in out["message"]

    def test_helper_requires_account(self, monkeypatch):
        from app.tools import amazon_ads_reports as rep

        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: True)
        out = asyncio.run(rep.amazon_ads_performance_report())
        assert out["status"] == "error"
        assert "account is required" in out["message"]
        # No real account name leaked anywhere in the message.
        assert "Mellanni" not in out["message"]

    def test_helper_rejects_targeting(self, monkeypatch):
        from app.tools import amazon_ads_reports as rep

        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: True)
        out = asyncio.run(
            rep.amazon_ads_performance_report(
                account="acme", report="targeting"))
        assert out["status"] == "error" and "disabled" in out["message"]


class TestPollBudget:
    def test_default_and_clamp(self, monkeypatch):
        from app.tools import amazon_ads_reports as rep

        monkeypatch.setattr(rep, "_DEFAULT_POLL_SECONDS", 600)
        monkeypatch.setattr(auth.vault, "get", lambda k, d="": d)
        assert rep._resolve_poll_budget() == 600
        monkeypatch.setattr(
            auth.vault, "get",
            lambda k, d="": "99999" if "POLL" in k else d)
        assert rep._resolve_poll_budget() == rep._MAX_POLL_SECONDS
        monkeypatch.setattr(
            auth.vault, "get", lambda k, d="": "5" if "POLL" in k else d)
        assert rep._resolve_poll_budget() == rep._MIN_POLL_SECONDS
        monkeypatch.setattr(
            auth.vault, "get", lambda k, d="": "abc" if "POLL" in k else d)
        assert rep._resolve_poll_budget() == 600

    def test_poll_tries_positive(self, monkeypatch):
        from app.tools import amazon_ads_reports as rep

        monkeypatch.setattr(auth.vault, "get", lambda k, d="": d)
        assert rep._poll_tries() >= 1
        assert rep._poll_tries() == int(600 / rep._POLL_INTERVAL)

    def test_pending_result_is_sanitized(self):
        from app.tools.amazon_ads_reports import _pending_result

        out = _pending_result(
            account_label="Acme Test Account", marketplace="US",
            report_family="campaign", period="yesterday",
            start="2026-05-17", end="2026-05-17", waited_s=600)
        assert out["status"] == "pending"
        blob = repr(out)
        # No ids / URLs / tokens leaked.
        assert "amzn1" not in blob and "http" not in blob
        assert "Bearer" not in blob
        assert out["retry"]["report_family"] == "campaign"
        assert out["retry"]["date_range"] == {
            "start": "2026-05-17", "end": "2026-05-17"}


class TestVaultFileFallback:
    def test_get_falls_back_to_vault_file(self, monkeypatch, tmp_path):
        from deploy import vault as dv

        vdir = tmp_path / "vault"
        vdir.mkdir()
        vfile = vdir / "credentials.json"
        vfile.write_text('{"ADS_API_REFRESH_TOKEN": "rt-on-disk"}')
        monkeypatch.setattr(dv, "VAULT_DIR", str(vdir))
        monkeypatch.setattr(dv, "VAULT_FILE", str(vfile))
        monkeypatch.setattr(dv, "VAULT_LOCK", str(vdir / ".lock"))
        monkeypatch.setattr(dv, "VAULT_BACKUP", str(vdir / ".bak"))
        monkeypatch.delenv("ADS_API_REFRESH_TOKEN", raising=False)

        # Not in env → must read the file (the bug: previously returned "").
        assert dv.get("ADS_API_REFRESH_TOKEN") == "rt-on-disk"
        # And it hydrates env for the fast path.
        assert os.environ.get("ADS_API_REFRESH_TOKEN") == "rt-on-disk"
        assert dv.get("ADS_NOPE", "d") == "d"


class TestConfigKeys:
    def test_ads_keys_are_configurable(self):
        from app.app_utils.config import ALLOWED_CONFIG_KEYS

        for k in (
            "ADS_API_CLIENT_ID",
            "ADS_API_CLIENT_SECRET",
            "ADS_API_REFRESH_TOKEN",
            "ADS_API_REGION",
        ):
            assert k in ALLOWED_CONFIG_KEYS, k


class TestMintClassification:
    def _resp(self, status, payload):
        class _R:
            status_code = status

            def json(self_inner):
                return payload

        return _R()

    def _vault(self, monkeypatch):
        monkeypatch.setattr(
            auth.vault, "get",
            lambda k, d="": {
                "ADS_API_CLIENT_ID": "c",
                "ADS_API_CLIENT_SECRET": "s",
                "ADS_API_REFRESH_TOKEN": "r",
            }.get(k, d),
        )

    def test_invalid_grant_is_reauth(self, monkeypatch):
        self._vault(monkeypatch)
        monkeypatch.setattr(
            auth.httpx, "post",
            lambda *a, **k: self._resp(400, {"error": "invalid_grant"}))
        with pytest.raises(auth.AmazonAdsAuthError) as ei:
            auth._mint_access_token()
        assert ei.value.reauth_required is True
        assert ei.value.transient is False

    def test_5xx_is_transient_not_reauth(self, monkeypatch):
        self._vault(monkeypatch)
        monkeypatch.setattr(
            auth.httpx, "post", lambda *a, **k: self._resp(503, {}))
        with pytest.raises(auth.AmazonAdsAuthError) as ei:
            auth._mint_access_token()
        assert ei.value.transient is True
        assert ei.value.reauth_required is False

    def test_429_is_transient(self, monkeypatch):
        self._vault(monkeypatch)
        monkeypatch.setattr(
            auth.httpx, "post", lambda *a, **k: self._resp(429, {}))
        with pytest.raises(auth.AmazonAdsAuthError) as ei:
            auth._mint_access_token()
        assert ei.value.transient is True

    def test_network_error_is_transient(self, monkeypatch):
        self._vault(monkeypatch)

        def _boom(*a, **k):
            raise auth.httpx.ConnectError("down")

        monkeypatch.setattr(auth.httpx, "post", _boom)
        with pytest.raises(auth.AmazonAdsAuthError) as ei:
            auth._mint_access_token()
        assert ei.value.transient is True
        assert ei.value.reauth_required is False

    def test_missing_refresh_token_is_reauth(self, monkeypatch):
        monkeypatch.setattr(auth.vault, "get", lambda k, d="": d)
        with pytest.raises(auth.AmazonAdsAuthError) as ei:
            auth._mint_access_token()
        assert ei.value.reauth_required is True

    def test_success_returns_token(self, monkeypatch):
        self._vault(monkeypatch)
        monkeypatch.setattr(
            auth.httpx, "post",
            lambda *a, **k: self._resp(
                200, {"access_token": "AT", "expires_in": 3600}))
        assert auth._mint_access_token() == ("AT", 3600)


class TestToolset:
    def test_helper_function_tool_always_present(self, monkeypatch):
        # Deterministic helper must be exposed even when MCP degrades.
        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: False)
        ts = AmazonAdsToolset()
        tools = asyncio.run(ts.get_tools())
        names = {getattr(t, "name", "") for t in tools}
        assert "amazon_ads_performance_report" in names

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
        # Helper FunctionTool stays available even when MCP is degraded.
        assert {getattr(t, "name", "") for t in tools} == {
            "amazon_ads_performance_report"
        }

    def test_degrades_loudly_without_credentials(self, monkeypatch, caplog):
        monkeypatch.setattr(
            "app.tools.amazon_ads_auth.credentials_present", lambda: False
        )
        ts = AmazonAdsToolset()
        with caplog.at_level(logging.CRITICAL):
            tools = asyncio.run(ts.get_tools())
        assert {getattr(t, "name", "") for t in tools} == {
            "amazon_ads_performance_report"
        }
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
        assert {getattr(t, "name", "") for t in tools} == {
            "amazon_ads_performance_report"
        }
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
        assert {getattr(t, "name", "") for t in tools} == {
            "amazon_ads_performance_report"
        }
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
        # helper FunctionTool + the (faked) MCP tool
        names = {getattr(t, "name", "") for t in first}
        assert names == {"amazon_ads_performance_report",
                         "reporting-create_report"}

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
