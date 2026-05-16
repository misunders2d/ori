"""Phase 14 slice 3 — PausedPendingPolicy + cancellation.

Per ``docs/PHASE_14_PLAN.md`` §9.1 (the (α) fork ruling) +
the claude-reviewer slice-3 binding conditions 1-5 + the
``cancelled_reason`` conditions.

Three pinned guarantees:

1. **Q3 hash-stability** — adding
   ``FailurePolicy.paused_pending_policy`` does NOT drift any
   pre-phase-14 frozen-spec hash. Proven with the exact
   phase-9 ``template.args`` reconstruct-pre-amendment-shape
   technique (``test_models_schedule.test_hash_none_args_
   matches_pre_amendment_shape``) over a representative
   corpus (plain reminder + OneOff-template +
   source-driven/RecurringSeriesFromSource-shaped). A spec
   that DOES set ``cancel_pending`` produces a stable,
   deterministic, distinct (sorted-JSON) hash.

2. **Round-trip** (closes the Q4/Q7 reachability gap) —
   ``cancel_pending`` survives ``insert_schedule`` →
   fresh-conn ``get_schedule`` → ``_row_to_spec``
   (``failure=decode_json(failure_raw, FailurePolicy)``).
   ``storage/schedules.py`` is byte-untouched — the (α)
   structural win is that ``failure_json`` already
   round-trips ``FailurePolicy``.

3. **Pause honours the policy** — ``let_complete`` (default /
   None) leaves pending Runs untouched (pre-phase-14
   no-op, least-surprise); ``cancel_pending`` cancels them
   via the EXISTING archive cancel-pending seam with a
   ``run_cancelled`` payload reason ``schedule_paused``
   (distinct from archive's ``schedule_archived`` — §13
   audit-truth); the archive path stays byte-identical
   (default ``cancelled_reason``).

Reuses the shipped lifecycle-helper harness so the seed is
identical to the phase-7 lifecycle tests.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.v2.authoring.lifecycle import schedule_archive, schedule_pause
from app.v2.enums import (
    DeliveryFallbackPolicy,
    EventKind,
    FailureActionType,
    PausedPendingPolicy,
    RunStatus,
    ScheduleStatus,
)
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    TemplateRef,
    UserRef,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import OneOffTrigger
from app.v2.storage.schedules import get_schedule, insert_schedule
from tests.v2.test_authoring_lifecycle_helper import (
    _UTC_NOW,
    _conn,
    _counter_event_factory,
    _fixed_clock,
    _seed_run,
)


# ---------------------------------------------------------------------------
# Corpus builders — representative pre-phase-14 frozen-spec shapes
# ---------------------------------------------------------------------------


def _spec(
    *,
    schedule_id: str = "sched_alpha",
    template: TemplateRef | None = None,
    execution_plan_hash: str | None = None,
    paused_pending_policy: PausedPendingPolicy | None = None,
    status: ScheduleStatus = ScheduleStatus.ACTIVE,
) -> ScheduleSpec:
    return ScheduleSpec(
        id=schedule_id,
        owner=UserRef(platform="slack", user_id="U_OWNER"),
        description="weekly amazon summary digest",
        trigger=OneOffTrigger(at_iso_datetime=_UTC_NOW, timezone="UTC"),
        delivery=Delivery(
            target_session_id="C123",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN,
            paused_pending_policy=paused_pending_policy,
        ),
        audit=AuditPolicy(),
        status=status,
        execution_plan_hash=execution_plan_hash,
        template=template,
        authored_at=_UTC_NOW.isoformat(),
    )


def _corpus_unset() -> dict[str, ScheduleSpec]:
    """Plain reminder + OneOff-template + source-driven
    (RecurringSeriesFromSource-shaped) — all with the field
    UNSET, i.e. the pre-phase-14 on-disk shape."""
    return {
        "plain_reminder": _spec(schedule_id="plain_reminder"),
        "oneoff_template": _spec(
            schedule_id="oneoff_template",
            template=TemplateRef(
                name="OneOffReminder", version="1", args={"text": "hi"}
            ),
        ),
        "source_driven": _spec(
            schedule_id="source_driven",
            execution_plan_hash="a" * 64,
        ),
    }


def _reconstruct_pre_amendment_hash(spec: ScheduleSpec) -> str:
    """The phase-9 technique: build the canonical body, assert
    it carries NO ``paused_pending_policy`` key (so the bytes
    are exactly the pre-phase-14 on-disk shape), re-hash via
    the SAME json.dumps path the storage layer uses."""
    body = spec.canonical_body()
    assert "paused_pending_policy" not in body["failure"]
    return hashlib.sha256(
        json.dumps(
            body, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


# ===========================================================================
# Q3 — hash-stability corpus (condition 1 + 2)
# ===========================================================================


@pytest.mark.parametrize("name", ["plain_reminder", "oneoff_template",
                                  "source_driven"])
def test_unset_hashes_byte_identical_to_pre_amendment(name):
    """Every pre-phase-14 frozen spec hashes BYTE-IDENTICAL
    after the FailurePolicy field is added (field unset)."""
    spec = _corpus_unset()[name]
    assert spec.compute_hash() == _reconstruct_pre_amendment_hash(spec)


@pytest.mark.parametrize("name", ["plain_reminder", "oneoff_template",
                                  "source_driven"])
def test_explicit_let_complete_equals_unset_hash(name):
    """Explicit ``let_complete`` is elided exactly like unset
    (the default branch of the canonical_body strip) — same
    hash, key absent."""
    unset = _corpus_unset()[name]
    explicit = _spec(
        schedule_id=name,
        template=unset.template,
        execution_plan_hash=unset.execution_plan_hash,
        paused_pending_policy=PausedPendingPolicy.LET_COMPLETE,
    )
    body = explicit.canonical_body()
    assert "paused_pending_policy" not in body["failure"]
    assert explicit.compute_hash() == unset.compute_hash()


def test_cancel_pending_hash_stable_distinct_and_key_present():
    """A spec that sets ``cancel_pending`` keeps the key in the
    canonical body, hashes deterministically (sorted-JSON), and
    differs from the unset/default hash — a real behaviour
    change is a new version."""
    unset = _spec(schedule_id="plain_reminder")
    cancel = _spec(
        schedule_id="plain_reminder",
        paused_pending_policy=PausedPendingPolicy.CANCEL_PENDING,
    )
    body = cancel.canonical_body()
    assert body["failure"]["paused_pending_policy"] == "cancel_pending"
    # Deterministic across two computes.
    assert cancel.compute_hash() == cancel.compute_hash()
    # Distinct from the unset/default hash.
    assert cancel.compute_hash() != unset.compute_hash()


def test_strip_does_not_touch_other_failure_keys():
    """The nested strip pops ONLY paused_pending_policy — the
    rest of the failure dict is untouched (no collateral
    elision; NOT model_dump(exclude_defaults=))."""
    spec = _spec(schedule_id="plain_reminder")
    failure = spec.canonical_body()["failure"]
    assert failure["on_failure_action"] == "alert_admin"
    assert "retry_policy" in failure  # defaulted None, still present


# ===========================================================================
# Round-trip — condition 3 (Q4/Q7 reachability across fresh conn)
# ===========================================================================


def test_cancel_pending_survives_persistence_round_trip(tmp_path):
    conn = _conn(tmp_path)
    spec = _spec(
        schedule_id="rt_cancel",
        paused_pending_policy=PausedPendingPolicy.CANCEL_PENDING,
    ).with_fresh_hash()
    insert_schedule(conn, spec)
    conn.commit()
    conn.close()

    # Fresh connection — the post-restart / fresh-conn read.
    conn2 = _conn(tmp_path)
    loaded = get_schedule(conn2, "rt_cancel")
    conn2.close()
    assert loaded is not None
    assert (
        loaded.failure.paused_pending_policy
        == PausedPendingPolicy.CANCEL_PENDING
    )


def test_unset_round_trips_as_none(tmp_path):
    conn = _conn(tmp_path)
    spec = _spec(schedule_id="rt_unset").with_fresh_hash()
    insert_schedule(conn, spec)
    conn.commit()
    loaded = get_schedule(conn, "rt_unset")
    conn.close()
    assert loaded is not None
    assert loaded.failure.paused_pending_policy is None


def test_storage_schedules_byte_untouched():
    """The (α) structural win: failure_json already
    round-trips FailurePolicy, so storage/schedules.py needs
    NO change. Pin the column list is exactly the v001 set —
    a new column would break this and signal accidental DDL."""
    from app.v2.storage import schedules as sched_mod

    assert sched_mod._COLUMNS == (
        "id",
        "owner",
        "description",
        "trigger_json",
        "delivery_json",
        "failure_json",
        "audit_json",
        "status",
        "execution_plan_hash",
        "template_json",
        "authored_at",
        "parent_hash",
        "hash",
    )


# ===========================================================================
# Pause honours the policy — conditions 4 + cancelled_reason
# ===========================================================================


def _seed_active(conn, *, schedule_id, policy):
    spec = _spec(
        schedule_id=schedule_id,
        paused_pending_policy=policy,
        status=ScheduleStatus.ACTIVE,
    ).with_fresh_hash()
    insert_schedule(conn, spec)
    conn.commit()
    return spec


def _run_kinds(conn, schedule_id):
    rows = conn.execute(
        "SELECT kind, payload_json FROM events "
        "WHERE schedule_id = ? ORDER BY ts ASC",
        (schedule_id,),
    ).fetchall()
    return rows


def _run_status(conn, run_id):
    return conn.execute(
        "SELECT status FROM runs WHERE id = ?", (run_id,)
    ).fetchone()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy", [None, PausedPendingPolicy.LET_COMPLETE]
)
async def test_pause_let_complete_leaves_pending_untouched(
    tmp_path, policy
):
    """Default / explicit let_complete: pause is a no-op for
    in-flight work (pre-phase-14 behaviour, least-surprise)."""
    conn = _conn(tmp_path)
    _seed_active(conn, schedule_id="s_lc", policy=policy)
    _seed_run(conn, schedule_id="s_lc", run_id="r1")
    _seed_run(conn, schedule_id="s_lc", run_id="r2")

    resp = await schedule_pause(
        "s_lc",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )
    assert resp.status == "ok"
    assert _run_status(conn, "r1") == RunStatus.PENDING.value
    assert _run_status(conn, "r2") == RunStatus.PENDING.value
    kinds = [k for k, _ in _run_kinds(conn, "s_lc")]
    assert "schedule_paused" in kinds
    assert "run_cancelled" not in kinds
    conn.close()


@pytest.mark.asyncio
async def test_pause_cancel_pending_cancels_with_paused_reason(tmp_path):
    """cancel_pending: every pending Run is cancelled in the
    pause TX; run_cancelled payload reason == 'schedule_paused'
    (distinct from archive); reuses PENDING→CANCELLED."""
    conn = _conn(tmp_path)
    _seed_active(
        conn,
        schedule_id="s_cp",
        policy=PausedPendingPolicy.CANCEL_PENDING,
    )
    _seed_run(conn, schedule_id="s_cp", run_id="r1")
    _seed_run(conn, schedule_id="s_cp", run_id="r2")

    resp = await schedule_pause(
        "s_cp",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )
    assert resp.status == "ok"
    assert "cancelled 2 pending run(s)" in resp.message
    assert _run_status(conn, "r1") == RunStatus.CANCELLED.value
    assert _run_status(conn, "r2") == RunStatus.CANCELLED.value

    cancelled = [
        json.loads(p)
        for k, p in _run_kinds(conn, "s_cp")
        if k == EventKind.RUN_CANCELLED.value
    ]
    assert len(cancelled) == 2
    for payload in cancelled:
        assert payload["reason"] == "schedule_paused"
        assert payload["reason"] != "schedule_archived"
    conn.close()


@pytest.mark.asyncio
async def test_archive_cancel_reason_byte_identical(tmp_path):
    """The archive path is byte-identical to pre-phase-14: it
    does NOT pass cancelled_reason, so the default
    'schedule_archived' is written (parity pin — archive vs
    pause-cancel differ only in this payload reason)."""
    conn = _conn(tmp_path)
    _seed_active(conn, schedule_id="s_ar", policy=None)
    _seed_run(conn, schedule_id="s_ar", run_id="r1")

    resp = await schedule_archive(
        "s_ar",
        conn=conn,
        event_id_factory=_counter_event_factory(),
        clock=_fixed_clock,
    )
    assert resp.status == "ok"
    assert _run_status(conn, "r1") == RunStatus.CANCELLED.value
    cancelled = [
        json.loads(p)
        for k, p in _run_kinds(conn, "s_ar")
        if k == EventKind.RUN_CANCELLED.value
    ]
    assert len(cancelled) == 1
    assert cancelled[0]["reason"] == "schedule_archived"
    conn.close()
