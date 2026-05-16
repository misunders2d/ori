"""End-to-end pin: the v2 scheduler's first working schedule.

Phase 9 slice 9 per ``docs/PHASE_9_PLAN.md`` §3.9 / §5.7.

This is the cutover proof. It wires the WHOLE phase-9 chain
in ONE test, with no mocks between the seams:

    schedule_create_reminder closure   (the tool the
        ↓                               CoordinatorAgent
    DB row + schedule_created event     mounts via
        ↓                               app.v2.wiring)
    wakeup(...)  ← invoked SYNCHRONOUSLY (NOT real
        ↓          APScheduler timing, per round-1
    pending Run + run_created event     reviewer Q10)
        ↓
    Worker.tick()  → claim → emit
        ↓
    stub Slack chat_postMessage + run_succeeded

Plus the round-1 reviewer L201 defence-in-depth pin: a
schedule deleted between Run insert and the emit branch's
re-fetch surfaces ``schedule_not_found_at_claim`` (no Slack
call, Run left RUNNING for recovery).

The only stub is the Slack transport (recording double).
Everything else — drafts, handshakes, validation, dry-run,
freeze, commit, the SQLite state DB, wakeup, the worker
claim loop, the emit branch — is the real production code
path. A freezeable clock is threaded through the closure,
wakeup and worker so ``at`` can be advanced past the fire
instant deterministically.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.authoring.drafts import DraftStore
from app.v2.authoring.handshake import HandshakeStore
from app.v2.authoring.templates import make_schedule_create_reminder
from app.v2.enums import EventKind, FireReason, RunStatus
from app.v2.migrations import runner
from app.v2.models.common import UserRef
from app.v2.models.run import Run
from app.v2.registry_cache.schemas import (
    SlackChannelEntry,
    SlackChannelsCache,
)
from app.v2.runtime.wakeup import wakeup
from app.v2.runtime.worker import UnsupportedSpecError, Worker
from app.v2.storage.events import (
    list_events_for_run,
    list_events_for_schedule,
)
from app.v2.storage.schedules import get_schedule

# Single tenant / owner for the bot's own authoring flow,
# matching app.v2.wiring's single-tenant phase-9 wiring.
_OWNER_ID = "T_TEST"
_SCHEDULE_ID = "sched_e2e_alpha"
_CHANNEL_NAME = "general"
_CHANNEL_ID = "C012ABCDE"
_REMINDER_TEXT = "stand-up in 5"

_T0 = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
_AT = _T0 + timedelta(hours=1)
# Wall-clock position used for wakeup + worker tick — past
# the OneOff fire instant so the trigger is due.
_FIRE_TIME = _T0 + timedelta(hours=2)


class _FreezeClock:
    """Mutable injectable clock. The closure reads it at
    authoring time (``at`` must be in the future); the test
    then advances it past ``at`` before driving wakeup +
    the worker."""

    def __init__(self, t: datetime) -> None:
        self._t = t

    def now(self) -> datetime:
        return self._t

    def advance_to(self, t: datetime) -> None:
        self._t = t


class _StubSlackClient:
    """Recording double for the Slack transport — the ONLY
    stub in the chain."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat_postMessage(self, *, channel: str, text: str):
        self.calls.append({"channel": channel, "text": text})
        return {"ok": True, "ts": "1700000000.000100"}


def _populated_cache() -> SlackChannelsCache:
    return SlackChannelsCache(
        workspace_id=_OWNER_ID,
        fetched_at=_T0 - timedelta(minutes=5),
        source="stub",
        channels=[
            SlackChannelEntry(
                id=_CHANNEL_ID,
                name=_CHANNEL_NAME,
                is_archived=False,
            ),
        ],
    )


def _shared_event_ids():
    """One monotonic id stream shared by the closure, wakeup
    and the worker so every events.id is globally unique
    across the whole end-to-end run (events.id is a PK)."""
    counter = {"i": 0}

    def factory() -> str:
        counter["i"] += 1
        return f"evt-{counter['i']:08d}-1111-1111-1111-111111111111"

    return factory


def _conn_factory(tmp_path: Path):
    db = tmp_path / "scheduler.db"
    init = sqlite3.connect(str(db))
    runner.apply_pending(init)
    init.close()

    def factory() -> sqlite3.Connection:
        c = sqlite3.connect(str(db))
        c.execute("PRAGMA foreign_keys=ON")
        return c

    return factory


def _build_closure(tmp_path: Path, clock: _FreezeClock, event_ids):
    drafts = DraftStore(base=tmp_path / "drafts")
    handshakes = HandshakeStore(base=tmp_path / "handshakes")
    factory = _conn_factory(tmp_path)
    cache = _populated_cache()

    closure = make_schedule_create_reminder(
        store=drafts,
        handshake_store=handshakes,
        conn_factory=factory,
        cache_loader=lambda: cache,
        cache_saver=lambda fresh: None,
        slack_client=None,  # cache pre-populated; no channels-list call
        expected_owner_id=_OWNER_ID,
        clock=clock.now,
        event_id_factory=event_ids,
        schedule_id_factory=lambda: _SCHEDULE_ID,
        owner=UserRef(
            platform="slack",
            user_id="U_OWNER",
            display_name="Sergey",
        ),
        session_id="sess_e2e",
    )
    return closure, factory


def _read_run_status(factory, run_id: str) -> str:
    conn = factory()
    try:
        row = conn.execute(
            "SELECT status FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def _make_worker(factory, clock: _FreezeClock, slack_client, event_ids):
    return Worker(
        conn_factory=factory,
        worker_id="worker-e2e",
        poll_interval=timedelta(seconds=10),
        clock=clock.now,
        run_id_factory=lambda: "should-not-be-called",
        event_id_factory=event_ids,
        slack_client=slack_client,
    )


# ===========================================================================
# Full happy-path sequence
# ===========================================================================


@pytest.mark.asyncio
async def test_e2e_one_off_reminder_happy_path(tmp_path):
    clock = _FreezeClock(_T0)
    event_ids = _shared_event_ids()
    closure, factory = _build_closure(tmp_path, clock, event_ids)

    # --- 1. Tool call: author the OneOff reminder. -------------
    response = await closure(_AT.isoformat(), _CHANNEL_NAME, _REMINDER_TEXT)

    assert response.status == "ok", f"unexpected: {response!r}"
    assert response.schedule_id == _SCHEDULE_ID
    assert response.spec["template"]["name"] == "OneOffReminder"
    assert response.spec["template"]["args"] == {"text": _REMINDER_TEXT}
    # Channel name resolved to its Slack id as the delivery
    # target.
    assert response.spec["delivery"]["target_session_id"] == _CHANNEL_ID

    # --- 2. DB row + schedule_created event present. -----------
    conn = factory()
    try:
        assert get_schedule(conn, _SCHEDULE_ID) is not None
        sched_events = list_events_for_schedule(conn, _SCHEDULE_ID)
        assert any(
            e.kind is EventKind.SCHEDULE_CREATED for e in sched_events
        )
    finally:
        conn.close()

    # --- 3. Advance the clock past ``at``, then invoke the
    #        wakeup callback SYNCHRONOUSLY (no real APScheduler
    #        timing — round-1 reviewer Q10). --------------------
    clock.advance_to(_FIRE_TIME)
    conn = factory()
    try:
        inserted = wakeup(
            conn,
            schedule_id=_SCHEDULE_ID,
            now=clock.now(),
            run_id_factory=lambda: "run-e2e-1",
            event_id_factory=event_ids,
        )
    finally:
        conn.close()

    assert inserted == ["run-e2e-1"]
    assert _read_run_status(factory, "run-e2e-1") == "pending"

    # --- 4. Worker claims the due Run, dispatches the emit
    #        branch, posts to Slack, writes run_succeeded. ------
    client = _StubSlackClient()
    worker = _make_worker(factory, clock, client, event_ids)
    claimed = await worker.tick()

    assert claimed == "run-e2e-1"
    assert _read_run_status(factory, "run-e2e-1") == "succeeded"

    # The reminder hit the resolved channel with the authored
    # text — exactly once.
    assert client.calls == [
        {"channel": _CHANNEL_ID, "text": _REMINDER_TEXT}
    ]

    run_kinds = [e.kind for e in _read_events(factory, "run-e2e-1")]
    assert EventKind.RUN_CLAIMED in run_kinds
    assert EventKind.RUN_STARTED in run_kinds
    assert EventKind.RUN_SUCCEEDED in run_kinds


def _read_events(factory, run_id: str):
    conn = factory()
    try:
        return list_events_for_run(conn, run_id)
    finally:
        conn.close()


# ===========================================================================
# L201 defence-in-depth: schedule deleted between Run insert
# and the emit branch's schedule re-fetch.
# ===========================================================================


@pytest.mark.asyncio
async def test_e2e_schedule_not_found_at_claim(tmp_path):
    """Same authored-by-the-real-closure schedule, but the
    schedule row is deleted after the Run is inserted (the
    race the emit branch's defensive re-fetch guards). The
    branch raises ``UnsupportedSpecError`` carrying
    ``schedule_not_found_at_claim``; no Slack call; the Run
    is left RUNNING for the phase-4 recovery path to
    promote."""
    clock = _FreezeClock(_T0)
    event_ids = _shared_event_ids()
    closure, factory = _build_closure(tmp_path, clock, event_ids)

    response = await closure(_AT.isoformat(), _CHANNEL_NAME, _REMINDER_TEXT)
    assert response.status == "ok"

    clock.advance_to(_FIRE_TIME)
    conn = factory()
    try:
        inserted = wakeup(
            conn,
            schedule_id=_SCHEDULE_ID,
            now=clock.now(),
            run_id_factory=lambda: "run-e2e-1",
            event_id_factory=event_ids,
        )
    finally:
        conn.close()
    assert inserted == ["run-e2e-1"]

    # Move the Run to RUNNING (the post-claim state the
    # worker leaves it in before dispatching to the emit
    # branch), then delete the schedule with the FK
    # temporarily off to simulate the race.
    conn = factory()
    try:
        conn.execute(
            "UPDATE runs SET status = 'running' WHERE id = ?",
            ("run-e2e-1",),
        )
        conn.commit()
    finally:
        conn.close()

    conn = factory()
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "DELETE FROM schedules WHERE id = ?", (_SCHEDULE_ID,)
        )
        conn.commit()
    finally:
        conn.close()

    client = _StubSlackClient()
    worker = _make_worker(factory, clock, client, event_ids)
    run = Run(
        id="run-e2e-1",
        schedule_id=_SCHEDULE_ID,
        execution_plan_hash=None,
        fire_reason=FireReason.SCHEDULED,
        due_at=_FIRE_TIME,
        status=RunStatus.RUNNING,
        attempt=1,
        root_run_id="run-e2e-1",
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

    assert client.calls == []
    assert _read_run_status(factory, "run-e2e-1") == "running"
