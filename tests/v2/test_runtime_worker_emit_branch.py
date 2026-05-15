"""Tests for the phase-9 worker emit branch.

Phase 9 slice 5 per ``docs/PHASE_9_PLAN.md`` §5.4.

Pins:
- Happy path: OneOffReminder spec → emit fires →
  run_succeeded; Run.status == succeeded; stub Slack
  client receives one chat_postMessage.
- Schedule-fetch + staleness (round-1 reviewer L201):
  - schedule deleted between Run insert + claim → emit
    `run_failed(schedule_not_found_at_claim)`; no Slack
    call.
  - archived schedule → `run_failed(schedule_inactive_at_claim)`.
  - paused schedule → same code as archived.
  - template-name drift (spec rebuilt with a different
    template name) → `run_failed(schedule_template_
    changed_at_claim)`.
- FailurePolicy routing:
  - alert_admin → emit_failed + admin_alert_sent +
    run_failed events all present.
  - abort_silent → emit_failed + run_failed; NO
    admin_alert_sent.
  - retry_later → WARNING log + downgrade to
    alert_admin semantics (admin_alert_sent event
    present with `downgrade_from`).
- UnsupportedSpec branches:
  - template=None AND execution_plan_hash=None →
    `UnsupportedSpecError`.
  - execution_plan_hash set → `UnsupportedSpecError`.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.emit.slack_reminder import SlackPostResult
from app.v2.enums import (
    DeliveryFallbackPolicy,
    EventKind,
    FailureActionType,
    FireReason,
    RunStatus,
    ScheduleStatus,
)
from app.v2.migrations import runner
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    TemplateRef,
    UserRef,
)
from app.v2.models.run import Run
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import OneOffTrigger
from app.v2.runtime.worker import (
    UnsupportedSpecError,
    Worker,
)
from app.v2.storage.events import list_events_for_run
from app.v2.storage.runs import insert_run
from app.v2.storage.schedules import insert_schedule


_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Stubs / fixtures
# ---------------------------------------------------------------------------


class _StubSlackClient:
    def __init__(
        self,
        *,
        response: dict | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self.response = response if response is not None else {
            "ok": True,
            "ts": "1700000000.000100",
        }
        self.raise_exc = raise_exc

    async def chat_postMessage(self, *, channel: str, text: str):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append({"channel": channel, "text": text})
        return self.response


def _conn_factory(tmp_path: Path):
    db = tmp_path / "scheduler.db"
    conn = sqlite3.connect(str(db))
    runner.apply_pending(conn)
    conn.close()

    def factory() -> sqlite3.Connection:
        c = sqlite3.connect(str(db))
        c.execute("PRAGMA foreign_keys=ON")
        return c

    return factory, db


def _build_spec(
    *,
    schedule_id: str = "sched_alpha",
    status: ScheduleStatus = ScheduleStatus.ACTIVE,
    template_name: str | None = "OneOffReminder",
    template_args: dict | None = None,
    failure_action: FailureActionType = FailureActionType.ALERT_ADMIN,
    execution_plan_hash: str | None = None,
    channel_id: str = "C012ABCDE",
) -> ScheduleSpec:
    if template_args is None:
        template_args = {"text": "weekly digest reminder"}
    if template_name is None:
        template = None
    else:
        template = TemplateRef(
            name=template_name,
            version="1",
            args=template_args,
        )
    spec = ScheduleSpec(
        id=schedule_id,
        description=f"OneOffReminder: {template_args.get('text', '')}",
        owner=UserRef(
            platform="slack",
            user_id="U_OWNER",
            display_name="Sergey",
        ),
        trigger=OneOffTrigger(
            at_iso_datetime=_NOW + timedelta(hours=1),
            timezone="UTC",
        ),
        delivery=Delivery(
            target_session_id=channel_id,
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(on_failure_action=failure_action),
        audit=AuditPolicy(),
        status=status,
        execution_plan_hash=execution_plan_hash,
        template=template,
        authored_at=_NOW.isoformat(),
    )
    return spec.with_fresh_hash()


def _seed_schedule_and_run(
    factory,
    *,
    spec: ScheduleSpec,
    run_id: str = "run-abc",
) -> Run:
    conn = factory()
    try:
        insert_schedule(conn, spec)
        run = Run(
            id=run_id,
            schedule_id=spec.id,
            execution_plan_hash=spec.execution_plan_hash,
            fire_reason=FireReason.SCHEDULED,
            due_at=_NOW,
            status=RunStatus.PENDING,
            attempt=1,
            root_run_id=run_id,
        )
        insert_run(conn, run)
        conn.commit()
        return run
    finally:
        conn.close()


def _make_worker(factory, slack_client) -> Worker:
    evt_counter = {"i": 0}

    def evt_factory() -> str:
        evt_counter["i"] += 1
        return f"evt-{evt_counter['i']:08d}-1111-1111-1111-111111111111"

    return Worker(
        conn_factory=factory,
        worker_id="worker-1",
        poll_interval=timedelta(seconds=10),
        clock=lambda: _NOW,
        run_id_factory=lambda: "should-not-be-called",
        event_id_factory=evt_factory,
        slack_client=slack_client,
    )


def _read_run_status(factory, run_id: str) -> str:
    conn = factory()
    try:
        row = conn.execute(
            "SELECT status FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def _read_events(factory, run_id: str):
    conn = factory()
    try:
        return list_events_for_run(conn, run_id)
    finally:
        conn.close()


# ===========================================================================
# Happy path
# ===========================================================================


@pytest.mark.asyncio
async def test_happy_path_emits_and_writes_run_succeeded(tmp_path):
    factory, _ = _conn_factory(tmp_path)
    spec = _build_spec()
    _seed_schedule_and_run(factory, spec=spec)
    client = _StubSlackClient()

    worker = _make_worker(factory, client)
    run_id = await worker.tick()

    assert run_id == "run-abc"
    assert _read_run_status(factory, "run-abc") == "succeeded"
    # Stub Slack received one call.
    assert len(client.calls) == 1
    assert client.calls[0]["channel"] == "C012ABCDE"
    assert client.calls[0]["text"] == "weekly digest reminder"
    # Events: claimed → running → succeeded.
    kinds = [e.kind for e in _read_events(factory, "run-abc")]
    assert EventKind.RUN_CLAIMED in kinds
    assert EventKind.RUN_STARTED in kinds
    assert EventKind.RUN_SUCCEEDED in kinds


# ===========================================================================
# Schedule fetch / staleness (L201 fix)
# ===========================================================================


@pytest.mark.asyncio
async def test_schedule_not_found_at_claim_raises_unsupported(tmp_path):
    """Defence-in-depth pin: when ``get_schedule`` returns
    ``None`` between claim and emit, the emit branch raises
    ``UnsupportedSpecError`` carrying the reason
    ``schedule_not_found_at_claim`` verbatim. The events
    table's FK on schedule_id makes writing a run_failed
    event impossible (the missing schedule row would fail
    the FK at INSERT), so the branch raises instead and
    leaves the Run in RUNNING for boot recovery to promote
    via the phase-4 stale-claim path.

    The race cannot be reproduced via ``worker.tick()`` —
    ``list_claimable_due`` joins on the schedule row, so a
    Run for a missing schedule is never claimable. We
    exercise the branch directly on
    ``_dispatch_emit_branch``."""
    factory, _ = _conn_factory(tmp_path)
    spec = _build_spec()
    _seed_schedule_and_run(factory, spec=spec)

    # Move the Run into RUNNING state (matches the post-
    # claim state the worker leaves the Run in before
    # dispatching to the emit branch). Then delete the
    # schedule (with FK off, since the Run holds a
    # reference) to simulate the race.
    conn = factory()
    try:
        conn.execute(
            "UPDATE runs SET status = 'running' WHERE id = ?",
            ("run-abc",),
        )
        conn.commit()
    finally:
        conn.close()

    # PRAGMA foreign_keys can only flip when no TX is
    # active — open a fresh conn outside the previous
    # transaction.
    conn = factory()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "DELETE FROM schedules WHERE id = ?", (spec.id,)
        )
        conn.commit()
    finally:
        conn.close()

    client = _StubSlackClient()
    worker = _make_worker(factory, client)

    run = Run(
        id="run-abc",
        schedule_id=spec.id,
        execution_plan_hash=None,
        fire_reason=FireReason.SCHEDULED,
        due_at=_NOW,
        status=RunStatus.RUNNING,
        attempt=1,
        root_run_id="run-abc",
    )

    conn = factory()
    try:
        with pytest.raises(
            UnsupportedSpecError,
            match="schedule_not_found_at_claim",
        ):
            await worker._dispatch_emit_branch(conn, run)
    finally:
        conn.close()

    # No Slack call.
    assert client.calls == []
    # Run stays in RUNNING (recovery promotes).
    assert _read_run_status(factory, "run-abc") == "running"


async def _dispatch_against_spec_status(
    tmp_path: Path, target_status: str
):
    """Helper: insert an ACTIVE spec, seed a RUNNING Run,
    then update the schedule to ``target_status`` before
    dispatching directly. ``list_claimable_due`` filters
    inactive schedules out of normal tick() flow, so we
    exercise ``_dispatch_emit_branch`` directly."""
    factory, _ = _conn_factory(tmp_path)
    spec = _build_spec(status=ScheduleStatus.ACTIVE)
    _seed_schedule_and_run(factory, spec=spec)

    conn = factory()
    try:
        conn.execute(
            "UPDATE runs SET status = 'running' WHERE id = ?",
            ("run-abc",),
        )
        conn.execute(
            "UPDATE schedules SET status = ? WHERE id = ?",
            (target_status, spec.id),
        )
        conn.commit()
    finally:
        conn.close()

    client = _StubSlackClient()
    worker = _make_worker(factory, client)
    run = Run(
        id="run-abc",
        schedule_id=spec.id,
        execution_plan_hash=None,
        fire_reason=FireReason.SCHEDULED,
        due_at=_NOW,
        status=RunStatus.RUNNING,
        attempt=1,
        root_run_id="run-abc",
    )
    conn = factory()
    try:
        outcome = await worker._dispatch_emit_branch(conn, run)
        conn.commit()
    finally:
        conn.close()
    return outcome, client, factory


@pytest.mark.asyncio
async def test_archived_schedule_run_failed(tmp_path):
    outcome, client, factory = await _dispatch_against_spec_status(
        tmp_path, "archived"
    )

    assert outcome == "failed"
    assert client.calls == []
    failed = [
        e
        for e in _read_events(factory, "run-abc")
        if e.kind is EventKind.RUN_FAILED
    ]
    assert len(failed) == 1
    assert (
        failed[0].payload["reason"] == "schedule_inactive_at_claim"
    )


@pytest.mark.asyncio
async def test_paused_schedule_run_failed(tmp_path):
    outcome, client, factory = await _dispatch_against_spec_status(
        tmp_path, "paused"
    )

    assert outcome == "failed"
    assert client.calls == []
    failed = [
        e
        for e in _read_events(factory, "run-abc")
        if e.kind is EventKind.RUN_FAILED
    ]
    assert (
        failed[0].payload["reason"] == "schedule_inactive_at_claim"
    )


@pytest.mark.asyncio
async def test_template_name_drift_run_failed(tmp_path):
    """Spec inserted with template `RecurringSeriesFromSource`;
    worker only knows `OneOffReminder` → drift code. Dispatch
    directly because the Run for this spec would be claimed
    by tick() (schedule is ACTIVE), but we want a clean unit
    pin on the dispatch logic."""
    factory, _ = _conn_factory(tmp_path)
    spec = _build_spec(template_name="RecurringSeriesFromSource")
    _seed_schedule_and_run(factory, spec=spec)

    conn = factory()
    try:
        conn.execute(
            "UPDATE runs SET status = 'running' WHERE id = ?",
            ("run-abc",),
        )
        conn.commit()
    finally:
        conn.close()

    client = _StubSlackClient()
    worker = _make_worker(factory, client)
    run = Run(
        id="run-abc",
        schedule_id=spec.id,
        execution_plan_hash=None,
        fire_reason=FireReason.SCHEDULED,
        due_at=_NOW,
        status=RunStatus.RUNNING,
        attempt=1,
        root_run_id="run-abc",
    )
    conn = factory()
    try:
        outcome = await worker._dispatch_emit_branch(conn, run)
        conn.commit()
    finally:
        conn.close()

    assert outcome == "failed"
    assert client.calls == []
    failed = [
        e
        for e in _read_events(factory, "run-abc")
        if e.kind is EventKind.RUN_FAILED
    ]
    assert (
        failed[0].payload["reason"]
        == "schedule_template_changed_at_claim"
    )


# ===========================================================================
# FailurePolicy routing
# ===========================================================================


@pytest.mark.asyncio
async def test_alert_admin_writes_emit_failed_plus_admin_alert(tmp_path):
    factory, _ = _conn_factory(tmp_path)
    spec = _build_spec(failure_action=FailureActionType.ALERT_ADMIN)
    _seed_schedule_and_run(factory, spec=spec)

    client = _StubSlackClient(
        response={"ok": False, "error": "channel_not_found"}
    )
    worker = _make_worker(factory, client)
    await worker.tick()

    kinds = [e.kind for e in _read_events(factory, "run-abc")]
    assert EventKind.EMIT_FAILED in kinds
    assert EventKind.ADMIN_ALERT_SENT in kinds
    assert EventKind.RUN_FAILED in kinds
    assert _read_run_status(factory, "run-abc") == "failed"


@pytest.mark.asyncio
async def test_abort_silent_writes_no_admin_alert(tmp_path):
    factory, _ = _conn_factory(tmp_path)
    spec = _build_spec(failure_action=FailureActionType.ABORT_SILENT)
    _seed_schedule_and_run(factory, spec=spec)

    client = _StubSlackClient(
        response={"ok": False, "error": "channel_not_found"}
    )
    worker = _make_worker(factory, client)
    await worker.tick()

    kinds = [e.kind for e in _read_events(factory, "run-abc")]
    assert EventKind.EMIT_FAILED in kinds
    assert EventKind.RUN_FAILED in kinds
    assert EventKind.ADMIN_ALERT_SENT not in kinds


@pytest.mark.asyncio
async def test_retry_later_downgrades_to_admin_alert_with_warning(
    tmp_path, caplog
):
    factory, _ = _conn_factory(tmp_path)
    spec = _build_spec(failure_action=FailureActionType.RETRY_LATER)
    _seed_schedule_and_run(factory, spec=spec)

    client = _StubSlackClient(
        response={"ok": False, "error": "channel_not_found"}
    )
    worker = _make_worker(factory, client)
    with caplog.at_level(logging.WARNING, logger="app.v2.runtime.worker"):
        await worker.tick()

    kinds = [e.kind for e in _read_events(factory, "run-abc")]
    assert EventKind.ADMIN_ALERT_SENT in kinds
    admin = [
        e
        for e in _read_events(factory, "run-abc")
        if e.kind is EventKind.ADMIN_ALERT_SENT
    ][0]
    assert admin.payload.get("downgrade_from") == "retry_later"
    # WARNING log present.
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any(
        "phase 9 downgrades to alert_admin" in r.getMessage()
        for r in warnings
    )


# ===========================================================================
# Atomicity (round-3 reviewer slice-5 fix)
# ===========================================================================


@pytest.mark.asyncio
async def test_route_failure_policy_atomic_rollback_on_run_update_failure(
    tmp_path,
    monkeypatch,
):
    """Round-3 reviewer slice-5 fix: _route_failure_policy
    wraps emit_failed + admin_alert_sent + run UPDATE +
    run_failed in ONE transaction. If the run UPDATE
    raises mid-TX, every prior event row rolls back AND
    the Run stays in RUNNING.

    Pre-fix the helper appended emit_failed +
    admin_alert_sent via standalone calls, then dispatched
    to ``update_run_status_and_append_event`` which opened
    its OWN TX. A failure on the final write left the
    prior events committed → EventLedger / run status
    diverged."""
    factory, _ = _conn_factory(tmp_path)
    spec = _build_spec(failure_action=FailureActionType.ALERT_ADMIN)
    _seed_schedule_and_run(factory, spec=spec)

    # Move Run to RUNNING (matches post-claim state).
    conn = factory()
    try:
        conn.execute(
            "UPDATE runs SET status = 'running' WHERE id = ?",
            ("run-abc",),
        )
        conn.commit()
    finally:
        conn.close()

    # Monkeypatch conn.execute on the worker's connection
    # to raise specifically on the UPDATE runs statement
    # (which fires AFTER the emit_failed +
    # admin_alert_sent events). The transaction must
    # rollback every prior insert.
    from app.v2.runtime import worker as worker_mod

    real_append = worker_mod.append_event
    appended_before_failure: list[str] = []

    def _tracking_append(conn_arg, event):
        appended_before_failure.append(event.kind.value)
        return real_append(conn_arg, event)

    monkeypatch.setattr(
        worker_mod, "append_event", _tracking_append
    )

    client = _StubSlackClient(
        response={"ok": False, "error": "channel_not_found"}
    )
    worker = _make_worker(factory, client)
    run = Run(
        id="run-abc",
        schedule_id=spec.id,
        execution_plan_hash=None,
        fire_reason=FireReason.SCHEDULED,
        due_at=_NOW,
        status=RunStatus.RUNNING,
        attempt=1,
        root_run_id="run-abc",
    )

    # Wrapper class around the real sqlite3 connection
    # that raises on the UPDATE runs statement. sqlite3
    # Connection attributes are read-only so we can't
    # monkeypatch ``conn.execute`` directly; the wrapper
    # delegates every other attribute via __getattr__.
    class _FailingExecuteConn:
        def __init__(self, real_conn: sqlite3.Connection) -> None:
            object.__setattr__(self, "_real", real_conn)

        def execute(self, sql, *args, **kwargs):
            if "UPDATE runs" in sql:
                raise sqlite3.IntegrityError(
                    "simulated post-events failure"
                )
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def __setattr__(self, name, value):
            # ``transaction(conn)`` sets isolation_level; the
            # wrapper forwards to the real connection.
            if name == "_real":
                object.__setattr__(self, name, value)
            else:
                setattr(self._real, name, value)

    real_conn = factory()
    wrapped = _FailingExecuteConn(real_conn)
    try:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="simulated post-events failure",
        ):
            await worker._route_failure_policy(
                conn=wrapped,
                run=run,
                spec=spec,
                result=SlackPostResult(
                    ok=False,
                    channel=spec.delivery.target_session_id,
                    error="channel_not_found",
                ),
            )
    finally:
        real_conn.close()

    # Pin the in-flight tracking: append_event WAS called
    # for emit_failed + admin_alert_sent BEFORE the
    # UPDATE raised. If those weren't tracked the test
    # itself would not be exercising the rollback path.
    assert "emit_failed" in appended_before_failure
    assert "admin_alert_sent" in appended_before_failure

    # ROLLBACK assertion: zero event rows persisted for
    # the run despite the in-flight appends.
    persisted_events = _read_events(factory, "run-abc")
    persisted_kinds = {e.kind.value for e in persisted_events}
    assert "emit_failed" not in persisted_kinds, (
        f"emit_failed leaked through rollback: "
        f"{persisted_kinds!r}"
    )
    assert "admin_alert_sent" not in persisted_kinds, (
        f"admin_alert_sent leaked through rollback: "
        f"{persisted_kinds!r}"
    )
    assert "run_failed" not in persisted_kinds, (
        f"run_failed leaked through rollback: "
        f"{persisted_kinds!r}"
    )

    # Run status stays at RUNNING — caller / recovery
    # promotes it via the stale-claim path.
    assert _read_run_status(factory, "run-abc") == "running"


# ===========================================================================
# UnsupportedSpec branches
# ===========================================================================


@pytest.mark.asyncio
async def test_no_template_no_plan_raises_unsupported_spec(tmp_path):
    """CustomFlow spec (template=None, no plan) →
    UnsupportedSpecError. Spec stays ACTIVE so this could
    flow via tick(), but we dispatch directly for
    isolation."""
    factory, _ = _conn_factory(tmp_path)
    spec = _build_spec(
        template_name=None, execution_plan_hash=None
    )
    _seed_schedule_and_run(factory, spec=spec)

    conn = factory()
    try:
        conn.execute(
            "UPDATE runs SET status = 'running' WHERE id = ?",
            ("run-abc",),
        )
        conn.commit()
    finally:
        conn.close()

    client = _StubSlackClient()
    worker = _make_worker(factory, client)
    run = Run(
        id="run-abc",
        schedule_id=spec.id,
        execution_plan_hash=None,
        fire_reason=FireReason.SCHEDULED,
        due_at=_NOW,
        status=RunStatus.RUNNING,
        attempt=1,
        root_run_id="run-abc",
    )

    conn = factory()
    try:
        with pytest.raises(UnsupportedSpecError, match="no template"):
            await worker._dispatch_emit_branch(conn, run)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_execution_plan_hash_set_raises_unsupported_spec(tmp_path):
    """Spec with execution_plan_hash set → UnsupportedSpecError.
    Seed an execution_plans row first so the FK on
    schedules.execution_plan_hash is satisfied."""
    factory, _ = _conn_factory(tmp_path)

    plan_hash = "a" * 64

    conn = factory()
    try:
        conn.execute(
            "INSERT INTO execution_plans "
            "(hash, body_json, enforcement, authored_at, author) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                plan_hash,
                "{}",
                "strict",
                _NOW.isoformat(),
                "test_seed",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    spec = _build_spec(
        template_name="OneOffReminder",
        execution_plan_hash=plan_hash,
    )
    _seed_schedule_and_run(factory, spec=spec)

    # Move Run to RUNNING for the legal transition.
    conn = factory()
    try:
        conn.execute(
            "UPDATE runs SET status = 'running' WHERE id = ?",
            ("run-abc",),
        )
        conn.commit()
    finally:
        conn.close()

    client = _StubSlackClient()
    worker = _make_worker(factory, client)
    run = Run(
        id="run-abc",
        schedule_id=spec.id,
        execution_plan_hash=plan_hash,
        fire_reason=FireReason.SCHEDULED,
        due_at=_NOW,
        status=RunStatus.RUNNING,
        attempt=1,
        root_run_id="run-abc",
    )

    conn = factory()
    try:
        with pytest.raises(
            UnsupportedSpecError, match="execution_plan_hash"
        ):
            await worker._dispatch_emit_branch(conn, run)
    finally:
        conn.close()


# ===========================================================================
# Backwards-compat — no slack_client means no emit branch
# ===========================================================================


@pytest.mark.asyncio
async def test_no_slack_client_uses_phase4_empty_body(tmp_path):
    """Backwards-compat: worker without slack_client kwarg
    keeps the phase-4 empty-body behaviour and ignores the
    schedule shape entirely. Phase-4 worker tests rely on
    this."""
    factory, _ = _conn_factory(tmp_path)
    spec = _build_spec(template_name="RecurringSeriesFromSource")
    _seed_schedule_and_run(factory, spec=spec)

    worker = _make_worker(factory, slack_client=None)
    run_id = await worker.tick()

    # Empty body → succeeded, no failure.
    assert run_id == "run-abc"
    assert _read_run_status(factory, "run-abc") == "succeeded"
