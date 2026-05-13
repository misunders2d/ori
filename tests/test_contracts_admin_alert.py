"""Tests for the contract admin-alert module.

Pins the guarantees described in ``app/contracts/admin_alert.py``:

  - Every call appends to ``data/contract_failures.jsonl`` BEFORE
    attempting any transport. Disk wins if transport breaks.
  - ``_resolve_chat_id`` resolves via the roster (canonical + tg_
    prefix variants) and falls back to interpreting the id itself as
    a chat_id (Telegram private chats: chat_id == user_id).
  - Direct Telegram send is HTTP-based, no adapter chain. Returns
    delivered=True only on ``HTTP 200 + ok=true``.
  - When 0/N admins receive the alert (empty env / all sends fail),
    a second ``alert_transport_failed`` line is written to the disk
    log AND a CRITICAL log is emitted.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest


@pytest.fixture
def alert_env(tmp_path, monkeypatch):
    """Isolate the failure log to a per-test file and reset env."""
    from app.contracts import admin_alert

    log_path = tmp_path / "contract_failures.jsonl"
    monkeypatch.setattr(admin_alert, "_FAILURE_LOG_PATH", str(log_path))
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return log_path


def _read_jsonl(path) -> list[dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# Admin id parsing
# ---------------------------------------------------------------------------


def test_admin_user_ids_empty_when_env_unset(alert_env, monkeypatch):
    from app.contracts.admin_alert import _admin_user_ids

    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    assert _admin_user_ids() == []


def test_admin_user_ids_parses_csv(alert_env, monkeypatch):
    from app.contracts.admin_alert import _admin_user_ids

    monkeypatch.setenv("ADMIN_USER_IDS", "330959414, tg_111222, sl_U0ABCDE ")
    assert _admin_user_ids() == ["330959414", "tg_111222", "sl_U0ABCDE"]


# ---------------------------------------------------------------------------
# chat_id resolution
# ---------------------------------------------------------------------------


def test_resolve_chat_id_via_roster_canonical(alert_env, monkeypatch):
    """Roster has the canonical user_id (``tg_<n>``) → use its
    chat_id."""
    from app.contracts import admin_alert

    monkeypatch.setattr(
        "app.core.roster.get_entry",
        lambda uid: {"chat_id": 444555666} if uid == "tg_330959414" else None,
    )
    assert admin_alert._resolve_chat_id("tg_330959414") == 444555666


def test_resolve_chat_id_via_roster_raw_numeric_lookup(alert_env, monkeypatch):
    """Caller passed a raw numeric, but the roster keys are
    ``tg_<n>``. The resolver tries both forms."""
    from app.contracts import admin_alert

    monkeypatch.setattr(
        "app.core.roster.get_entry",
        lambda uid: {"chat_id": 444555666} if uid == "tg_330959414" else None,
    )
    assert admin_alert._resolve_chat_id("330959414") == 444555666


def test_resolve_chat_id_numeric_fallback(alert_env, monkeypatch):
    """Roster miss but the id IS numeric — fall back to using the id
    itself as a chat_id (Telegram private chats: chat_id == user_id)."""
    from app.contracts import admin_alert

    monkeypatch.setattr("app.core.roster.get_entry", lambda uid: None)
    assert admin_alert._resolve_chat_id("330959414") == 330959414
    assert admin_alert._resolve_chat_id("tg_330959414") == 330959414


def test_resolve_chat_id_unresolvable_returns_none(alert_env, monkeypatch):
    from app.contracts import admin_alert

    monkeypatch.setattr("app.core.roster.get_entry", lambda uid: None)
    assert admin_alert._resolve_chat_id("sl_U0ABCDE") is None


# ---------------------------------------------------------------------------
# Disk persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_admins_writes_disk_log_before_transport(
    alert_env, monkeypatch
):
    """Even when there are zero admins to message, the failure record
    is still durably written to disk."""
    from app.contracts.admin_alert import notify_admins

    report = await notify_admins(
        "Test alert body",
        contract_id="x_test",
        phase="emit_failed",
        audit_path="/tmp/audit.jsonl",
        error="adapter rejected args",
    )

    rows = _read_jsonl(alert_env)
    assert len(rows) >= 1
    assert rows[0]["contract_id"] == "x_test"
    assert rows[0]["phase"] == "emit_failed"
    assert rows[0]["error"] == "adapter rejected args"
    assert report["delivered"] == 0
    assert report["attempted"] == 0


@pytest.mark.asyncio
async def test_notify_admins_logs_transport_failure_when_env_empty(
    alert_env, caplog, monkeypatch
):
    """ADMIN_USER_IDS env empty → second jsonl line marked
    ``alert_transport_failed`` so the disk log alone can explain why
    no human was notified."""
    from app.contracts.admin_alert import notify_admins

    with caplog.at_level("CRITICAL"):
        await notify_admins(
            "Test", contract_id="x_test", phase="on_failure", error="boom"
        )

    rows = _read_jsonl(alert_env)
    phases = [r.get("phase") for r in rows]
    assert "on_failure" in phases
    assert "alert_transport_failed" in phases

    transport_row = next(r for r in rows if r.get("phase") == "alert_transport_failed")
    assert "ADMIN_USER_IDS" in transport_row.get("reason", "")


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, status_code: int, body: dict | str):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if isinstance(body, dict) else body

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("not json")


class _StubAsyncClient:
    def __init__(self, response: _StubResponse):
        self._response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return self._response


def _install_httpx_stub(monkeypatch, response: _StubResponse) -> _StubAsyncClient:
    import httpx

    client = _StubAsyncClient(response)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    return client


@pytest.mark.asyncio
async def test_notify_admins_delivers_when_token_and_admin_set(
    alert_env, monkeypatch
):
    """Happy path: env wired, roster resolves chat_id, telegram returns
    200/ok=true → report.delivered = 1."""
    from app.contracts.admin_alert import notify_admins

    monkeypatch.setenv("ADMIN_USER_IDS", "330959414")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE:TOKEN")
    monkeypatch.setattr(
        "app.core.roster.get_entry",
        lambda uid: {"chat_id": 330959414} if uid == "tg_330959414" else None,
    )
    client = _install_httpx_stub(
        monkeypatch, _StubResponse(200, {"ok": True, "result": {}})
    )

    report = await notify_admins(
        "hello", contract_id="x_test", phase="on_failure", error="boom"
    )

    assert report["delivered"] == 1
    assert report["attempted"] == 1
    assert report["admins"][0]["chat_id"] == 330959414
    assert report["admins"][0]["ok"] is True
    # The httpx call used the env token + chat_id we expected.
    assert client.calls
    assert "FAKE:TOKEN" in client.calls[0]["url"]
    assert client.calls[0]["json"]["chat_id"] == 330959414


@pytest.mark.asyncio
async def test_notify_admins_counts_failure_when_telegram_returns_ok_false(
    alert_env, monkeypatch
):
    """Telegram API can return HTTP 200 with ``ok: false`` (e.g. bot
    blocked, chat_id invalid). Must NOT count as delivered — that was
    the exact 2026-05-13 silent-success failure mode."""
    from app.contracts.admin_alert import notify_admins

    monkeypatch.setenv("ADMIN_USER_IDS", "330959414")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE:TOKEN")
    monkeypatch.setattr(
        "app.core.roster.get_entry",
        lambda uid: {"chat_id": 999} if uid == "tg_330959414" else None,
    )
    _install_httpx_stub(
        monkeypatch,
        _StubResponse(200, {"ok": False, "description": "Forbidden: bot was blocked"}),
    )

    report = await notify_admins(
        "hi", contract_id="x_test", phase="on_failure", error="boom"
    )

    assert report["delivered"] == 0
    rows = _read_jsonl(alert_env)
    assert any(r.get("phase") == "alert_transport_failed" for r in rows)


@pytest.mark.asyncio
async def test_notify_admins_counts_failure_on_http_non_200(
    alert_env, monkeypatch
):
    from app.contracts.admin_alert import notify_admins

    monkeypatch.setenv("ADMIN_USER_IDS", "330959414")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE:TOKEN")
    monkeypatch.setattr(
        "app.core.roster.get_entry",
        lambda uid: {"chat_id": 999} if uid == "tg_330959414" else None,
    )
    _install_httpx_stub(monkeypatch, _StubResponse(401, {"error": "unauth"}))

    report = await notify_admins(
        "hi", contract_id="x_test", phase="on_failure", error="boom"
    )

    assert report["delivered"] == 0


@pytest.mark.asyncio
async def test_notify_admins_no_token_marks_failure(alert_env, monkeypatch):
    from app.contracts.admin_alert import notify_admins

    monkeypatch.setenv("ADMIN_USER_IDS", "330959414")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(
        "app.core.roster.get_entry",
        lambda uid: {"chat_id": 999} if uid == "tg_330959414" else None,
    )

    report = await notify_admins(
        "hi", contract_id="x_test", phase="on_failure", error="boom"
    )

    assert report["delivered"] == 0
    assert "TELEGRAM_BOT_TOKEN" in report["admins"][0]["detail"]


@pytest.mark.asyncio
async def test_notify_admins_unresolvable_user_reports_failure(
    alert_env, monkeypatch
):
    """A user_id that the roster can't resolve AND that isn't numeric
    (e.g. a Slack id) → report.delivered=0, per-admin detail explains."""
    from app.contracts.admin_alert import notify_admins

    monkeypatch.setenv("ADMIN_USER_IDS", "sl_U0ABCDE")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE:TOKEN")
    monkeypatch.setattr("app.core.roster.get_entry", lambda uid: None)

    report = await notify_admins(
        "hi", contract_id="x_test", phase="on_failure", error="boom"
    )

    assert report["delivered"] == 0
    assert report["admins"][0]["chat_id"] is None
    assert "could not resolve" in report["admins"][0]["detail"]


# ---------------------------------------------------------------------------
# Regression: numeric user_id used to silently FATAL because the
# telegram_dm adapter did a name lookup. New path treats the numeric
# id as a chat_id fallback and delivers cleanly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regression_numeric_admin_id_delivered_via_numeric_fallback(
    alert_env, monkeypatch
):
    """2026-05-13 incident repro. Roster has NO entry for the admin
    (poller hadn't recorded them under that key). The legacy
    ``telegram_dm`` adapter would lookup_by_name(person="330959414")
    → not_found → silent drop. The new path interprets the numeric
    id as a chat_id directly → delivers."""
    from app.contracts.admin_alert import notify_admins

    monkeypatch.setenv("ADMIN_USER_IDS", "330959414")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "FAKE:TOKEN")
    # Empty roster — exactly the conditions that broke the legacy path.
    monkeypatch.setattr("app.core.roster.get_entry", lambda uid: None)
    client = _install_httpx_stub(
        monkeypatch, _StubResponse(200, {"ok": True, "result": {}})
    )

    report = await notify_admins(
        "incident repro",
        contract_id="ai_pilot_wed_v3",
        phase="emit_failed",
        error="slack_post requires args.channel and args.content",
    )

    assert report["delivered"] == 1
    assert report["admins"][0]["chat_id"] == 330959414
    assert client.calls[0]["json"]["chat_id"] == 330959414
