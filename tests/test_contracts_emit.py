"""Emit adapter + gate registry tests.

Heavy adapters (slack_post, sheet_append) need live integrations to
fully exercise. Here we cover: registry shape, template rendering of
args, error surfacing on unknown adapters, gate truth values, and
duplicate-registration guard.

End-to-end tests with mocked Slack/Drive land in P7 alongside the
first real consumer contracts.
"""

from __future__ import annotations

import pytest

from app.contracts.emit import (
    EMIT_ADAPTERS,
    GATES,
    known_adapters,
    known_gates,
    register_adapter,
    register_gate,
    run_emit,
    run_gate,
)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_known_adapters_contains_full_v1_set():
    """Every adapter the AUTHOR pipeline currently knows about must be
    registered. New adapters extend this list."""
    expected = {
        "slack_post",
        "telegram_dm",
        "sheet_append",
        "drive_doc_fill",
        "email",
        "memory_update",
    }
    assert set(known_adapters()) >= expected


def test_known_gates_contains_v1_set():
    """Gates currently published. ``always_pass`` / ``always_fail`` are
    test-only conveniences but live in the registry so contracts that
    reference them by name validate cleanly."""
    expected = {"sheet_dedup", "always_pass", "always_fail"}
    assert set(known_gates()) >= expected


@pytest.mark.asyncio
async def test_run_emit_raises_keyerror_on_unknown():
    with pytest.raises(KeyError, match="unknown emit adapter"):
        await run_emit("ghost_adapter_42", {}, {})


@pytest.mark.asyncio
async def test_run_gate_raises_keyerror_on_unknown():
    with pytest.raises(KeyError, match="unknown gate"):
        await run_gate("ghost_gate_42", {}, {})


# ---------------------------------------------------------------------------
# Reserved slots
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,args",
    [
        ("sheet_append", {"spreadsheet_id": "x", "row": [1, 2, 3]}),
        ("drive_doc_fill", {"doc_id": "x", "fields": {}}),
        ("email", {"to": "x@y", "subject": "s", "body": "b"}),
        ("memory_update", {"title": "x"}),
    ],
)
@pytest.mark.asyncio
async def test_reserved_adapters_surface_not_implemented(name, args):
    with pytest.raises(NotImplementedError, match="P7"):
        await run_emit(name, args, {})


@pytest.mark.asyncio
async def test_reserved_sheet_dedup_gate_surfaces_not_implemented():
    with pytest.raises(NotImplementedError, match="P7"):
        await run_gate("sheet_dedup", {"source": "x", "key": "y"}, {})


# ---------------------------------------------------------------------------
# Always-pass / always-fail gates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_always_pass_returns_true():
    assert await run_gate("always_pass", {}, {}) is True


@pytest.mark.asyncio
async def test_always_fail_returns_false():
    assert await run_gate("always_fail", {}, {}) is False


# ---------------------------------------------------------------------------
# Adapter arg validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_post_requires_channel_and_content(monkeypatch):
    """Missing required args should surface a clear ValueError BEFORE
    we attempt the Slack call — keeps the failure local to the
    contract rather than bouncing through Slack's API error path."""
    with pytest.raises(ValueError, match="channel and args.content"):
        await run_emit("slack_post", {"channel": "#x"}, {})
    with pytest.raises(ValueError, match="channel and args.content"):
        await run_emit("slack_post", {"content": "hi"}, {})


@pytest.mark.asyncio
async def test_telegram_dm_requires_user_id_and_text():
    with pytest.raises(ValueError, match="user_id and args.text"):
        await run_emit("telegram_dm", {"user_id": "123"}, {})


# ---------------------------------------------------------------------------
# Registry dedup guard
# ---------------------------------------------------------------------------


def test_register_adapter_rejects_duplicates():
    @register_adapter("test_dup_adapter_42")
    async def _fn(args, state):
        return {}

    with pytest.raises(ValueError, match="already registered"):

        @register_adapter("test_dup_adapter_42")
        async def _fn2(args, state):
            return {}


def test_register_gate_rejects_duplicates():
    @register_gate("test_dup_gate_42")
    async def _fn(args, state):
        return True

    with pytest.raises(ValueError, match="already registered"):

        @register_gate("test_dup_gate_42")
        async def _fn2(args, state):
            return True


# ---------------------------------------------------------------------------
# Template rendering of args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_args_are_template_rendered_against_state():
    """Args containing {placeholders} resolve from state before the
    adapter sees them. Hook a dummy adapter to capture what args
    actually arrived."""
    captured = {}

    @register_adapter("test_capture_42")
    async def _capture(args, state):
        captured.update(args)
        return {"status": "ok"}

    state = {"asin": "B0XYZ", "today": "2026-05-11"}
    await run_emit(
        "test_capture_42",
        {"channel": "#audit-{asin}", "note": "Run on {today}"},
        state,
    )

    assert captured == {"channel": "#audit-B0XYZ", "note": "Run on 2026-05-11"}
