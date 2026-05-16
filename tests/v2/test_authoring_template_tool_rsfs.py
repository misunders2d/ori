"""Phase 11 slice 7b — schedule_create_recurring_series_from_source.

Per ``docs/PHASE_11_PLAN.md`` §3.4 / §5 + the
claude-reviewer 7b hard-checks. The authoring tool on the
7a-extended spine:

- LLM-visible signature = the slot set ONLY
  ``(source, channel, hour_local, timezone,
  progress_strategy)`` — every DI bound BEHIND it (the
  phase-9 ``make_schedule_create_reminder`` DI-leak pin).
- Funnels through the SAME (7a-extended) validate /
  dry-run / freeze / commit pipeline — NO bespoke commit.
- Reuses the slice-6 builder (inputs + emit, ZERO
  reasoning); the draft carries the FROZEN ExecutionPlan
  body so the 7a commit step-8b consistency check
  (``plan.hash == execution_plan_hash``) passes and the
  atomic ``insert_execution_plan``+``insert_schedule``
  path is exercised end to end.
- Bad slot rejected AT the tool boundary.
- The phase-9 OneOff authoring tool is byte-untouched
  (signature pin) — adding this factory to the same
  module did not perturb it.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from google.adk.tools.function_tool import FunctionTool

from app.v2.authoring.drafts import DraftStore
from app.v2.authoring.handshake import HandshakeStore
from app.v2.authoring.templates import (
    SCHEDULE_CREATE_RECURRING_SERIES_FROM_SOURCE_TOOL_NAME,
    make_schedule_create_recurring_series_from_source,
    make_schedule_create_reminder,
)
from app.v2.migrations import runner
from app.v2.models.common import UserRef
from app.v2.storage.execution_plans import get_execution_plan
from app.v2.storage.schedules import get_schedule

_T0 = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
_SLOTS = ["source", "channel", "hour_local", "timezone",
          "progress_strategy"]


def _clock() -> datetime:
    return _T0


def _owner() -> UserRef:
    return UserRef(platform="slack", user_id="U_OWN",
                   display_name="S")


def _migrate(tmp_path: Path):
    sqlite3.connect(str(tmp_path / "scheduler.db")).close()
    c = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(c)
    c.close()


def _build(tmp_path: Path):
    drafts = DraftStore(base=tmp_path / "drafts")
    handshakes = HandshakeStore(base=tmp_path / "handshakes")
    db_path = tmp_path / "scheduler.db"
    _migrate(tmp_path)
    ids = {"i": 0}

    def _eid() -> str:
        ids["i"] += 1
        return f"evt-{ids['i']:08d}-1111-1111-1111-111111111111"

    def _sid() -> str:
        return "sched_rsfs"

    def conn_factory() -> sqlite3.Connection:
        c = sqlite3.connect(str(db_path))
        c.execute("PRAGMA foreign_keys=ON")
        return c

    closure = make_schedule_create_recurring_series_from_source(
        store=drafts,
        handshake_store=handshakes,
        conn_factory=conn_factory,
        clock=_clock,
        event_id_factory=_eid,
        schedule_id_factory=_sid,
        owner=_owner(),
        session_id="sess1",
    )
    return closure, conn_factory


_GOOD_SOURCE = {
    "loader": "source_literal",
    "args": {"source_id": "src", "text": "hello"},
}


# ---------------------------------------------------------------------------
# DI-leak pin — LLM-visible signature is the slot set ONLY
# ---------------------------------------------------------------------------


def test_closure_signature_is_slot_set_only(tmp_path):
    closure, _ = _build(tmp_path)
    assert (
        list(inspect.signature(closure).parameters) == _SLOTS
    )


def test_function_tool_signature_is_slot_set_only(tmp_path):
    closure, _ = _build(tmp_path)
    tool = FunctionTool(func=closure)
    assert (
        list(inspect.signature(tool.func).parameters) == _SLOTS
    )


def test_closure_name_matches_constant(tmp_path):
    closure, _ = _build(tmp_path)
    assert (
        closure.__name__
        == SCHEDULE_CREATE_RECURRING_SERIES_FROM_SOURCE_TOOL_NAME
    )


def test_oneoff_tool_signature_byte_untouched(tmp_path):
    """The phase-9 OneOff factory in the SAME module is
    unperturbed by adding the RSFS factory — its
    LLM-visible signature is still exactly its slot set."""
    drafts = DraftStore(base=tmp_path / "d")
    handshakes = HandshakeStore(base=tmp_path / "h")
    oneoff = make_schedule_create_reminder(
        store=drafts,
        handshake_store=handshakes,
        conn_factory=lambda: sqlite3.connect(":memory:"),
        cache_loader=lambda: None,
        cache_saver=lambda _c: None,
        slack_client=None,
        expected_owner_id="T",
        clock=_clock,
        event_id_factory=lambda: "e",
        schedule_id_factory=lambda: "s",
        owner=_owner(),
        session_id="sess1",
    )
    assert list(inspect.signature(oneoff).parameters) == [
        "at",
        "recipient_channel",
        "text",
    ]


# ---------------------------------------------------------------------------
# Happy path — SAME pipeline, atomic plan+schedule (7a) end to end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_commits_plan_and_schedule(tmp_path):
    closure, conn_factory = _build(tmp_path)

    r = await closure(
        source=_GOOD_SOURCE,
        channel="C012ABCDE",
        hour_local=9,
        timezone="Europe/Kyiv",
        progress_strategy="skip_unchanged",
    )

    assert r.status == "ok", r
    conn = conn_factory()
    try:
        sched = get_schedule(conn, "sched_rsfs")
        assert sched is not None
        # The 7a atomic path ran: the ExecutionPlan body
        # was persisted, and the schedule points at it.
        assert sched.execution_plan_hash is not None
        plan = get_execution_plan(
            conn, sched.execution_plan_hash
        )
        assert plan is not None
        # Reuses the slice-6 builder → ZERO reasoning,
        # one source input, one source_post emit.
        assert plan.reasoning == []
        assert len(plan.inputs) == 1
        assert plan.inputs[0].source_ref is not None
        assert len(plan.emit) == 1
        assert plan.emit[0].adapter == "source_post"
        assert plan.emit[0].args["progress_strategy"] == (
            "skip_unchanged"
        )
        # hash == execution_plan_hash consistency held.
        assert plan.hash == sched.execution_plan_hash
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bad slot rejected AT the tool boundary (no pipeline entry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kw, expect_code",
    [
        (
            {"source": {"loader": "no_such_loader", "args": {}}},
            "unknown_source_loader",
        ),
        ({"source": "not-a-dict"}, "invalid_source"),
        (
            {"source": {"loader": "source_literal", "args": 1}},
            "invalid_source",
        ),
        ({"hour_local": 24}, "recurring_series_args_invalid"),
        ({"channel": ""}, "recurring_series_args_invalid"),
        ({"timezone": ""}, "recurring_series_args_invalid"),
        (
            {"progress_strategy": "every_blue_moon"},
            "recurring_series_args_invalid",
        ),
    ],
)
async def test_bad_slot_rejected_at_tool_boundary(
    tmp_path, kw, expect_code
):
    closure, conn_factory = _build(tmp_path)
    call = dict(
        source=_GOOD_SOURCE,
        channel="C012ABCDE",
        hour_local=9,
        timezone="UTC",
        progress_strategy="whole",
    )
    call.update(kw)

    r = await closure(**call)

    assert r.status == "validation_failed"
    assert expect_code in {i.code for i in r.issues}
    # No schedule landed.
    conn = conn_factory()
    try:
        assert get_schedule(conn, "sched_rsfs") is None
    finally:
        conn.close()
