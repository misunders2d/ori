"""Tests for ``app.v2.storage.schedules``.

Pins per ``docs/PHASE_3_PLAN.md`` §9.4:

- Insert + get round-trip (every Pydantic sub-object preserved
  by value, including the Trigger discriminated union and the
  optional TemplateRef).
- ``list_active_schedules`` returns only ``status='active'``
  rows, ordered by id.
- ``update_schedule_status`` flips the column and persists.
- FK enforcement: a bogus ``execution_plan_hash`` is accepted at
  insert time because the FK is comment-only at the schedule
  level (per design §4.0.2 resync). Pinning this so a future
  schema change makes the test fail loudly.
- CHECK enforcement: ``status='enabled'`` (not in the canonical
  set) is rejected with IntegrityError.
- ``insert_schedule`` refuses drafts (``hash=''``) with
  ``ScheduleNotFrozenError``.
- ``get_schedule`` returns ``None`` for missing ids.
- ``update_schedule_status`` raises ``ScheduleNotFoundError``
  for missing ids.

Smoke:
- Module imports no I/O libs.
- No execution / claim-suggestive public callables.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
    OnOversizePolicy,
    RetryStrategy,
    ScheduleStatus,
)
from app.v2.migrations import runner
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    RetryPolicy,
    TemplateRef,
    UserRef,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import CronTrigger, OneOffTrigger
from app.v2.storage import schedules as schedules_mod
from app.v2.storage.connection import ConnectionNotReady
from app.v2.storage.schedules import (
    ScheduleNotFoundError,
    ScheduleNotFrozenError,
    get_schedule,
    insert_schedule,
    list_active_schedules,
    update_schedule_status,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


_NOW_ISO = "2026-05-15T09:00:00+00:00"


def _migrate(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "scheduler.db"))
    runner.apply_pending(conn)
    return conn


def _baseline_spec(**overrides) -> ScheduleSpec:
    base = dict(
        id="daily_audit",
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="daily fba audit roll-up",
        trigger=CronTrigger(cron="0 18 * * *", timezone="Europe/Kyiv"),
        delivery=Delivery(
            target_session_id="sl_C0B2LJRS8D8",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN,
        ),
        audit=AuditPolicy(),
        status=ScheduleStatus.ACTIVE,
        execution_plan_hash="a" * 64,
        template=None,
        authored_at=_NOW_ISO,
        parent_hash=None,
    )
    base.update(overrides)
    return ScheduleSpec(**base).with_fresh_hash()


# ===========================================================================
# Insert + get round-trip
# ===========================================================================


def test_insert_and_get_round_trip(tmp_path):
    conn = _migrate(tmp_path)
    spec = _baseline_spec()
    returned_id = insert_schedule(conn, spec)
    assert returned_id == spec.id

    fetched = get_schedule(conn, spec.id)
    assert fetched is not None
    assert fetched == spec


def test_round_trip_preserves_owner_userref(tmp_path):
    """The DB stores ``owner`` as JSON of UserRef so the
    round-trip preserves platform / user_id / display_name."""
    conn = _migrate(tmp_path)
    spec = _baseline_spec(
        owner=UserRef(
            platform="slack",
            user_id="U07ABCDEF",
            display_name="Alice",
        ),
    )
    insert_schedule(conn, spec)
    fetched = get_schedule(conn, spec.id)
    assert fetched.owner == spec.owner


def test_round_trip_preserves_one_off_trigger(tmp_path):
    """Trigger is a discriminated union; pin that a OneOffTrigger
    round-trips as OneOffTrigger (not the default CronTrigger)."""
    conn = _migrate(tmp_path)
    spec = _baseline_spec(
        trigger=OneOffTrigger(
            at_iso_datetime="2026-06-01T09:00:00+00:00",
        ),
        execution_plan_hash=None,
    )
    insert_schedule(conn, spec)
    fetched = get_schedule(conn, spec.id)
    assert isinstance(fetched.trigger, OneOffTrigger)
    assert fetched.trigger == spec.trigger


def test_round_trip_preserves_cron_trigger_fields(tmp_path):
    """CronTrigger has cron + timezone — pin both."""
    conn = _migrate(tmp_path)
    spec = _baseline_spec(
        trigger=CronTrigger(
            cron="*/5 9-17 * * MON-FRI",
            timezone="America/Los_Angeles",
        ),
    )
    insert_schedule(conn, spec)
    fetched = get_schedule(conn, spec.id)
    assert isinstance(fetched.trigger, CronTrigger)
    assert fetched.trigger.cron == "*/5 9-17 * * MON-FRI"
    assert fetched.trigger.timezone == "America/Los_Angeles"


def test_round_trip_preserves_failure_with_retry_policy(tmp_path):
    """FailurePolicy.retry_policy is an Optional sub-model;
    confirm the nested RetryPolicy survives."""
    conn = _migrate(tmp_path)
    spec = _baseline_spec(
        failure=FailurePolicy(
            on_failure_action=FailureActionType.RETRY_LATER,
            retry_policy=RetryPolicy(
                strategy=RetryStrategy.EXPONENTIAL,
                base_seconds=60,
                max_attempts=3,
            ),
        ),
    )
    insert_schedule(conn, spec)
    fetched = get_schedule(conn, spec.id)
    assert fetched.failure == spec.failure


def test_round_trip_preserves_audit_policy(tmp_path):
    conn = _migrate(tmp_path)
    spec = _baseline_spec(
        audit=AuditPolicy(
            keep_last_n_snapshots=7,
            dedup_by_content_hash=False,
            redact_fields=["email", "phone"],
            max_snapshot_bytes=500_000,
            on_oversize=OnOversizePolicy.REDACT_AND_STORE,
        ),
    )
    insert_schedule(conn, spec)
    fetched = get_schedule(conn, spec.id)
    assert fetched.audit == spec.audit


def test_round_trip_preserves_template_when_set(tmp_path):
    conn = _migrate(tmp_path)
    spec = _baseline_spec(
        template=TemplateRef(name="recurring_series", version="2"),
    )
    insert_schedule(conn, spec)
    fetched = get_schedule(conn, spec.id)
    assert fetched.template == TemplateRef(
        name="recurring_series", version="2"
    )


def test_round_trip_preserves_null_template(tmp_path):
    conn = _migrate(tmp_path)
    spec = _baseline_spec(template=None)
    insert_schedule(conn, spec)
    fetched = get_schedule(conn, spec.id)
    assert fetched.template is None


def test_round_trip_preserves_parent_hash(tmp_path):
    conn = _migrate(tmp_path)
    parent = "b" * 64
    spec = _baseline_spec(parent_hash=parent)
    insert_schedule(conn, spec)
    fetched = get_schedule(conn, spec.id)
    assert fetched.parent_hash == parent


def test_round_trip_preserves_null_execution_plan_hash(tmp_path):
    """Reminder-style spec — OneOff + no plan."""
    conn = _migrate(tmp_path)
    spec = _baseline_spec(
        trigger=OneOffTrigger(at_iso_datetime="2026-06-01T09:00:00+00:00"),
        execution_plan_hash=None,
    )
    insert_schedule(conn, spec)
    fetched = get_schedule(conn, spec.id)
    assert fetched.execution_plan_hash is None


# ===========================================================================
# get_schedule edge cases
# ===========================================================================


def test_get_missing_id_returns_none(tmp_path):
    conn = _migrate(tmp_path)
    assert get_schedule(conn, "no_such_id") is None


# ===========================================================================
# list_active_schedules
# ===========================================================================


def test_list_active_returns_only_active(tmp_path):
    conn = _migrate(tmp_path)
    insert_schedule(conn, _baseline_spec(id="a_active"))
    insert_schedule(
        conn, _baseline_spec(id="b_paused", status=ScheduleStatus.PAUSED)
    )
    insert_schedule(conn, _baseline_spec(id="c_active"))
    insert_schedule(
        conn, _baseline_spec(id="d_archived", status=ScheduleStatus.ARCHIVED)
    )

    active = list_active_schedules(conn)
    ids = [s.id for s in active]
    assert ids == ["a_active", "c_active"]


def test_list_active_empty_db(tmp_path):
    conn = _migrate(tmp_path)
    assert list_active_schedules(conn) == []


def test_list_active_orders_by_id(tmp_path):
    conn = _migrate(tmp_path)
    insert_schedule(conn, _baseline_spec(id="zebra"))
    insert_schedule(conn, _baseline_spec(id="alpha"))
    insert_schedule(conn, _baseline_spec(id="mike"))
    active = list_active_schedules(conn)
    assert [s.id for s in active] == ["alpha", "mike", "zebra"]


# ===========================================================================
# update_schedule_status
# ===========================================================================


def test_update_status_active_to_paused(tmp_path):
    conn = _migrate(tmp_path)
    spec = _baseline_spec()
    insert_schedule(conn, spec)
    update_schedule_status(conn, spec.id, ScheduleStatus.PAUSED)
    fetched = get_schedule(conn, spec.id)
    assert fetched.status is ScheduleStatus.PAUSED


def test_update_status_to_archived(tmp_path):
    conn = _migrate(tmp_path)
    spec = _baseline_spec()
    insert_schedule(conn, spec)
    update_schedule_status(conn, spec.id, ScheduleStatus.ARCHIVED)
    fetched = get_schedule(conn, spec.id)
    assert fetched.status is ScheduleStatus.ARCHIVED


def test_update_status_missing_id_raises(tmp_path):
    conn = _migrate(tmp_path)
    with pytest.raises(ScheduleNotFoundError, match="ghost"):
        update_schedule_status(conn, "ghost", ScheduleStatus.PAUSED)


def test_update_status_removes_from_active_list(tmp_path):
    conn = _migrate(tmp_path)
    insert_schedule(conn, _baseline_spec(id="will_pause"))
    insert_schedule(conn, _baseline_spec(id="will_stay"))
    update_schedule_status(conn, "will_pause", ScheduleStatus.PAUSED)
    active = list_active_schedules(conn)
    assert [s.id for s in active] == ["will_stay"]


# ===========================================================================
# insert_schedule guards
# ===========================================================================


def test_insert_refuses_draft_without_hash(tmp_path):
    """Storage layer never persists a draft. The freeze pathway
    is responsible for ``with_fresh_hash()`` before this helper
    sees the spec."""
    conn = _migrate(tmp_path)
    spec = ScheduleSpec(
        id="draft",
        owner=UserRef(platform="telegram", user_id="1"),
        description="long enough description for the validator",
        trigger=OneOffTrigger(at_iso_datetime="2026-06-01T09:00:00+00:00"),
        delivery=Delivery(
            target_session_id="sl_X",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(),
        audit=AuditPolicy(),
    )  # NO with_fresh_hash() — hash stays ''
    assert spec.hash == ""
    with pytest.raises(ScheduleNotFrozenError, match="draft"):
        insert_schedule(conn, spec)


def test_insert_duplicate_id_raises_integrity_error(tmp_path):
    conn = _migrate(tmp_path)
    insert_schedule(conn, _baseline_spec(id="dup"))
    with pytest.raises(sqlite3.IntegrityError):
        # Different description so the hashes differ; the
        # uniqueness violation is on the primary key.
        insert_schedule(
            conn,
            _baseline_spec(id="dup", description="another version of dup"),
        )


def test_insert_duplicate_hash_raises_integrity_error(tmp_path):
    """``hash`` has a UNIQUE constraint in the DDL — two
    distinct ids with the same hash must be rejected."""
    conn = _migrate(tmp_path)
    spec_a = _baseline_spec(id="a")
    spec_b = _baseline_spec(id="b").model_copy(update={"hash": spec_a.hash})
    insert_schedule(conn, spec_a)
    with pytest.raises(sqlite3.IntegrityError):
        insert_schedule(conn, spec_b)


def test_insert_executes_plan_hash_with_no_plan_table_row_is_accepted(tmp_path):
    """Design §4.0.2 (post-resync) keeps ``execution_plan_hash``
    as a comment-only FK at the schedule level — i.e. no
    declared FOREIGN KEY clause. Pin that behavior so a future
    DDL change that adds the FK forces the test to update
    in lockstep with the storage layer."""
    conn = _migrate(tmp_path)
    spec = _baseline_spec(execution_plan_hash="0" * 64)
    # No execution_plans row exists for this hash — insert
    # succeeds anyway because the constraint is comment-only.
    insert_schedule(conn, spec)
    fetched = get_schedule(conn, spec.id)
    assert fetched.execution_plan_hash == "0" * 64


# ===========================================================================
# CHECK enforcement
# ===========================================================================


def test_status_check_rejects_unknown_value(tmp_path):
    """``status='enabled'`` is not in the canonical CHECK set
    (active/paused/archived). The Pydantic ScheduleStatus enum
    already rejects it at construction, so we bypass via raw
    SQL to exercise the DDL constraint directly."""
    conn = _migrate(tmp_path)
    spec = _baseline_spec()
    insert_schedule(conn, spec)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE schedules SET status = 'enabled' WHERE id = ?",
            (spec.id,),
        )


# ===========================================================================
# Connection guard
# ===========================================================================


def test_insert_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        insert_schedule(bare, _baseline_spec())


def test_get_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        get_schedule(bare, "any")


def test_list_active_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        list_active_schedules(bare)


def test_update_status_calls_assert_connection_ready(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    with pytest.raises(ConnectionNotReady):
        update_schedule_status(bare, "any", ScheduleStatus.PAUSED)


# ===========================================================================
# Smoke checks
# ===========================================================================


def test_schedules_module_has_no_io_imports():
    forbidden = {
        "httpx",
        "requests",
        "urllib.request",
        "urllib3",
        "aiohttp",
        "slack_sdk",
        "telegram",
        "googleapiclient",
        "google.cloud",
        "smtplib",
        "subprocess",
    }
    seen = set()
    for _, member in vars(schedules_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"schedules module imports I/O libs: {sorted(leaked)}."
    )


def test_schedules_module_has_no_dispatch_callables():
    forbidden = {
        "dispatch",
        "invoke",
        "call",
        "execute",
        "run",
        "send",
        "start",
        "loop",
        "worker",
        "claim",
        "claim_run",
    }
    for name, member in vars(schedules_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"schedules module exposes execution / claim-suggestive "
                f"callable: {name}"
            )
