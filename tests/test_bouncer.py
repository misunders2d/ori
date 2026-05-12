"""Stuck-agent bouncer tests.

Covers the three callbacks in ``app/callbacks/guardrails/bouncer.py``:

- ``on_tool_error_bouncer`` arms the bounce flag on tool-not-found or
  on a tool that has failed ``_MAX_REPEATED_ERRORS`` times in a row.
- ``force_bounce_before_model`` synthesises a ``transfer_to_agent``
  function_call LlmResponse when the flag is armed.
- ``reset_error_history_after_tool`` clears the counter on success.

Why this matters (2026-05-12 transcript): AmazonAgent hallucinated
``analyze_data``, ADK emitted a verbose "Tool not found" dump, the
model retried the same call 3 turns in a row. ROUTING FALLBACK is a
soft prompt; under stress the LLM ignores it. This module makes the
escape deterministic.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from google.genai import types

from app.callbacks.guardrails.bouncer import (
    _ERROR_HIST_KEY,
    _FORCE_BOUNCE_KEY,
    _MAX_REPEATED_ERRORS,
    force_bounce_before_model,
    on_tool_error_bouncer,
    reset_error_history_after_tool,
)


def _make_state_ctx(initial: dict | None = None):
    class _S:
        def __init__(self):
            self._d = dict(initial or {})

        def get(self, k, default=None):
            return self._d.get(k, default)

        def __setitem__(self, k, v):
            self._d[k] = v

        def __getitem__(self, k):
            return self._d[k]

    ctx = MagicMock()
    ctx.state = _S()
    return ctx


def _make_tool(name: str):
    t = MagicMock()
    t.name = name
    return t


def test_tool_not_found_arms_bounce_immediately():
    ctx = _make_state_ctx()
    err = ValueError(
        "Tool 'analyze_data' not found.\nAvailable tools: x, y\n\nPossible causes: ..."
    )
    out = on_tool_error_bouncer(
        tool=_make_tool("analyze_data"),
        args={},
        tool_context=ctx,
        error=err,
    )
    assert ctx.state.get(_FORCE_BOUNCE_KEY) is True
    assert "CoordinatorAgent" in out["message"]
    assert out["status"] == "error"


def test_single_legit_error_does_not_bounce():
    """A one-off error from a real tool (e.g. SP-API 429) must NOT
    bounce — the agent should be allowed to retry / report normally."""
    ctx = _make_state_ctx()
    err = RuntimeError("rate limited")
    out = on_tool_error_bouncer(
        tool=_make_tool("sp_get_catalog_item"),
        args={"asin": "B0X"},
        tool_context=ctx,
        error=err,
    )
    assert ctx.state.get(_FORCE_BOUNCE_KEY) is not True
    assert out is None  # let ADK re-raise


def test_repeated_errors_eventually_bounce():
    ctx = _make_state_ctx()
    err = RuntimeError("timeout")
    for _ in range(_MAX_REPEATED_ERRORS):
        out = on_tool_error_bouncer(
            tool=_make_tool("sp_get_catalog_item"),
            args={},
            tool_context=ctx,
            error=err,
        )
    assert ctx.state.get(_FORCE_BOUNCE_KEY) is True
    assert "CoordinatorAgent" in out["message"]


def test_force_bounce_synthesises_transfer_function_call():
    ctx = _make_state_ctx({_FORCE_BOUNCE_KEY: True, _ERROR_HIST_KEY: {"x": 5}})
    out = force_bounce_before_model(callback_context=ctx, llm_request=MagicMock())
    assert out is not None
    parts = out.content.parts
    assert len(parts) == 1
    fc = parts[0].function_call
    assert fc.name == "transfer_to_agent"
    assert fc.args == {"agent_name": "CoordinatorAgent"}
    # Flag + counter cleared so Coordinator starts clean.
    assert ctx.state.get(_FORCE_BOUNCE_KEY) is False
    assert ctx.state.get(_ERROR_HIST_KEY) == {}


def test_force_bounce_no_op_when_flag_absent():
    ctx = _make_state_ctx()
    out = force_bounce_before_model(callback_context=ctx, llm_request=MagicMock())
    assert out is None


def test_reset_clears_history_on_success():
    ctx = _make_state_ctx({_ERROR_HIST_KEY: {"x": 3}})
    reset_error_history_after_tool(
        tool=_make_tool("x"),
        args={},
        tool_context=ctx,
        tool_response={"status": "success", "data": "ok"},
    )
    assert ctx.state.get(_ERROR_HIST_KEY) == {}


def test_reset_preserves_history_on_error():
    """If the tool returned status=error, the next failure should still
    count toward the bounce threshold — don't reset on errors."""
    ctx = _make_state_ctx({_ERROR_HIST_KEY: {"x": 1}})
    reset_error_history_after_tool(
        tool=_make_tool("x"),
        args={},
        tool_context=ctx,
        tool_response={"status": "error", "message": "bad input"},
    )
    assert ctx.state.get(_ERROR_HIST_KEY) == {"x": 1}
