"""Pending-followup guard tests — the after-tool callback that
prevents the "I'll get back to you" lie.

The guard annotates the response of any tool in
``_LONG_RUNNING_SUBMIT_TOOLS`` so the agent's next LLM turn sees an
explicit `__followup_required__` block. Without that, the agent would
make verbal promises about future deliveries that never fire — exactly
the 2026-05-14 "give me the sales today!" incident.

Coverage:
- Long-running tool success → guard injects ``__followup_required__``
- Same tool name but error response → guard passes through (nothing to
  follow up on)
- Tool not in the long-running set → guard passes through
- Schedule / status-check tools (exempt) → guard passes through
- Non-dict response → guard passes through (no shape to annotate)
- Op-id key resolution priority + missing op-id case
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.callbacks.guardrails import (
    _LONG_RUNNING_SUBMIT_TOOLS,
    _FOLLOWUP_EXEMPT_TOOLS,
    pending_followup_guard,
)


def _tool(name: str) -> MagicMock:
    m = MagicMock()
    m.name = name
    return m


def _ctx(state_dict=None) -> MagicMock:
    state = MagicMock()
    state.to_dict.return_value = state_dict or {}
    ctx = MagicMock()
    ctx.state = state
    return ctx


# ---------------------------------------------------------------------------
# Happy path — long-running submission annotated
# ---------------------------------------------------------------------------


def test_sp_request_report_success_gets_annotated():
    """A successful SP-API report submission must be annotated so the
    agent sees the must-schedule-followup instruction."""
    resp = {
        "status": "success",
        "report_id": "3765960020584",
        "message": "Report requested.",
    }
    annotated = pending_followup_guard(
        tool=_tool("sp_request_report"),
        args={},
        tool_context=_ctx(),
        tool_response=resp,
    )
    assert annotated is not None
    assert annotated["__followup_required__"]["operation_id"] == "3765960020584"
    assert annotated["__followup_required__"]["operation_id_key"] == "report_id"
    assert annotated["__followup_required__"]["tool_called"] == "sp_request_report"
    assert "MUST call schedule_one_off_task" in annotated["__followup_required__"]["instruction"]
    # Suggested steps include the polling discipline (DONE / IN_PROGRESS /
    # FATAL / max-attempts branches).
    steps = annotated["__followup_required__"]["suggested_steps"]
    assert len(steps) >= 4
    assert any("DONE" in s for s in steps)
    assert any("IN_PROGRESS" in s for s in steps)
    assert any("FATAL" in s for s in steps)


def test_followup_inherits_deliver_to_from_state():
    """The follow-up hint pulls the originating channel from session
    state so the agent doesn't have to re-figure it out."""
    resp = {"status": "success", "report_id": "R1"}
    state = {"deliver_to_session": "sl_C0B2LJRS8D8"}
    annotated = pending_followup_guard(
        tool=_tool("sp_request_report"),
        args={},
        tool_context=_ctx(state),
        tool_response=resp,
    )
    assert annotated["__followup_required__"]["deliver_to_hint"] == "sl_C0B2LJRS8D8"


# ---------------------------------------------------------------------------
# Pass-through paths
# ---------------------------------------------------------------------------


def test_error_response_unchanged():
    """If the submission itself failed, there's nothing to follow up
    on — the agent should report the error verbatim to the user."""
    resp = {"status": "error", "message": "SP-API credentials missing."}
    out = pending_followup_guard(
        tool=_tool("sp_request_report"),
        args={},
        tool_context=_ctx(),
        tool_response=resp,
    )
    assert out is None


def test_unknown_tool_unchanged():
    """Tools not registered in ``_LONG_RUNNING_SUBMIT_TOOLS`` are not
    annotated — most tools return their result synchronously."""
    resp = {"status": "success", "report_id": "wat"}
    out = pending_followup_guard(
        tool=_tool("random_synchronous_tool"),
        args={},
        tool_context=_ctx(),
        tool_response=resp,
    )
    assert out is None


def test_exempt_tools_unchanged():
    """Schedule + status-check tools must be exempt — otherwise the
    follow-up's own scheduled call would trigger an infinite annotate
    loop (and the agent would re-schedule a self-check of the
    self-check, which is nonsense)."""
    for exempt in ("schedule_one_off_task", "sp_check_report"):
        assert exempt in _FOLLOWUP_EXEMPT_TOOLS
        resp = {"status": "success", "report_id": "R1"}
        out = pending_followup_guard(
            tool=_tool(exempt),
            args={},
            tool_context=_ctx(),
            tool_response=resp,
        )
        assert out is None, f"exempt tool {exempt} should not be annotated"


def test_non_dict_response_unchanged():
    """Some tools return strings or other non-dict shapes; guard must
    not crash and must not annotate (no place to put the annotation)."""
    out = pending_followup_guard(
        tool=_tool("sp_request_report"),
        args={},
        tool_context=_ctx(),
        tool_response="just a string",
    )
    assert out is None


def test_success_without_op_id_unchanged():
    """A long-running tool that returned success but no recognisable
    op-id key can't be followed up — guard skips rather than
    annotating with a placeholder."""
    resp = {"status": "success", "message": "no ids here"}
    out = pending_followup_guard(
        tool=_tool("sp_request_report"),
        args={},
        tool_context=_ctx(),
        tool_response=resp,
    )
    assert out is None


# ---------------------------------------------------------------------------
# Op-id resolution priority
# ---------------------------------------------------------------------------


def test_op_id_resolution_prefers_report_id_first():
    """Resolution order: report_id, operation_id, job_id, request_id,
    task_id. First match wins. A response that has multiple ids should
    surface the most specific one."""
    resp = {
        "status": "success",
        "report_id": "R1",
        "job_id": "J1",
        "task_id": "T1",
    }
    annotated = pending_followup_guard(
        tool=_tool("sp_request_report"),
        args={},
        tool_context=_ctx(),
        tool_response=resp,
    )
    assert annotated["__followup_required__"]["operation_id"] == "R1"


def test_response_data_preserved():
    """Annotation is additive — the original keys must still be
    readable by other consumers (the LLM, downstream callbacks).
    Replacing instead of adding would break those."""
    resp = {
        "status": "success",
        "report_id": "R1",
        "message": "Original message stays.",
        "extra_data": {"nested": True},
    }
    annotated = pending_followup_guard(
        tool=_tool("sp_request_report"),
        args={},
        tool_context=_ctx(),
        tool_response=resp,
    )
    assert annotated["status"] == "success"
    assert annotated["report_id"] == "R1"
    assert annotated["message"] == "Original message stays."
    assert annotated["extra_data"] == {"nested": True}


# ---------------------------------------------------------------------------
# Registry sanity — keep the long-running set + exempt set non-overlapping
# ---------------------------------------------------------------------------


def test_long_running_and_exempt_sets_are_disjoint():
    """A tool can't be both 'long-running submit' and 'exempt' — that
    would mean we annotate AND we don't, which the test would flag if
    they ever drifted into overlap. Disjoint by construction; this
    test pins the invariant."""
    assert not (_LONG_RUNNING_SUBMIT_TOOLS & _FOLLOWUP_EXEMPT_TOOLS)
