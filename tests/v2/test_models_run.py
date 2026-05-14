"""Tests for ``app.v2.models.run``.

Pins:
- Status / fire_reason constrained to enum values.
- attempt = 1 ⟺ parent_run_id is None.
- attempt = 1 ⟹ root_run_id == id (self-reference).
- attempt >= 2 ⟹ parent_run_id set AND root_run_id != id.
- ``retry_pending`` is NOT a valid RunStatus (round-6
  correction).
- extra fields rejected.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0
- ``docs/PHASE_1_PLAN.md`` §4.6 / §5.1
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.v2.enums import FireReason, RunStatus
from app.v2.models.run import Run


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


def _first_attempt_kwargs(**overrides):
    """Build a valid first-attempt Run. ``root_run_id`` defaults
    to the same value as ``id`` (self-reference)."""
    run_id = overrides.get("id", "11111111-1111-1111-1111-111111111111")
    base = dict(
        id=run_id,
        schedule_id="daily_audit",
        execution_plan_hash="a" * 64,
        fire_reason=FireReason.SCHEDULED,
        due_at=_NOW,
        status=RunStatus.PENDING,
        attempt=1,
        root_run_id=run_id,
        parent_run_id=None,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


def test_status_defaults_to_pending():
    """Sanity: a Run constructed without an explicit status
    starts at pending. Worker pool transitions it forward."""
    kwargs = _first_attempt_kwargs()
    kwargs.pop("status")
    r = Run(**kwargs)
    assert r.status == RunStatus.PENDING


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.PENDING,
        RunStatus.CLAIMED,
        RunStatus.RUNNING,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    ],
)
def test_status_accepts_all_canonical_values(status):
    r = Run(**_first_attempt_kwargs(status=status))
    assert r.status == status


def test_status_rejects_retry_pending():
    """The 'retry_pending' string used to be a RunStatus value
    in early drafts of v2. Round-6 collapsed retries into new
    pending Runs and removed the state. Re-introducing it would
    break the retry-chain invariants documented in
    ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.1."""
    with pytest.raises(ValidationError):
        Run(**_first_attempt_kwargs(status="retry_pending"))


def test_status_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Run(**_first_attempt_kwargs(status="frozen"))


# ---------------------------------------------------------------------------
# fire_reason enum
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        FireReason.SCHEDULED,
        FireReason.MANUAL,
        FireReason.RETRY,
        FireReason.REPLAY,
        FireReason.BACKFILL,
    ],
)
def test_fire_reason_accepts_all_canonical_values(reason):
    r = Run(**_first_attempt_kwargs(fire_reason=reason))
    assert r.fire_reason == reason


def test_fire_reason_rejects_unknown():
    with pytest.raises(ValidationError):
        Run(**_first_attempt_kwargs(fire_reason="cron_due"))


# ---------------------------------------------------------------------------
# attempt + root_run_id + parent_run_id invariants
# ---------------------------------------------------------------------------


def test_first_attempt_self_reference_ok():
    """attempt=1 + root_run_id=id + parent_run_id=None is the
    canonical first-attempt shape."""
    r = Run(**_first_attempt_kwargs())
    assert r.attempt == 1
    assert r.root_run_id == r.id
    assert r.parent_run_id is None


def test_first_attempt_with_parent_run_id_rejected():
    """A first attempt cannot have a parent — there's nothing
    to chain to."""
    with pytest.raises(ValidationError, match="parent_run_id=None"):
        Run(
            **_first_attempt_kwargs(
                parent_run_id="22222222-2222-2222-2222-222222222222"
            )
        )


def test_first_attempt_root_must_equal_id():
    """attempt=1 with a non-self root_run_id is rejected — the
    self-reference is what marks a row as 'the original'."""
    with pytest.raises(ValidationError, match="root_run_id == id"):
        Run(
            **_first_attempt_kwargs(
                root_run_id="22222222-2222-2222-2222-222222222222"
            )
        )


def test_retry_requires_parent_run_id():
    with pytest.raises(ValidationError, match="parent_run_id set"):
        Run(
            **_first_attempt_kwargs(
                attempt=2,
                root_run_id="11111111-1111-1111-1111-111111111111",
                parent_run_id=None,
            )
        )


def test_retry_root_must_not_equal_id():
    """A retry that names itself as root is malformed — the root
    is the FIRST attempt's id, not the retry's own."""
    retry_id = "22222222-2222-2222-2222-222222222222"
    with pytest.raises(ValidationError, match="self-reference"):
        Run(
            id=retry_id,
            schedule_id="daily_audit",
            execution_plan_hash="a" * 64,
            fire_reason=FireReason.RETRY,
            due_at=_NOW,
            status=RunStatus.PENDING,
            attempt=2,
            root_run_id=retry_id,
            parent_run_id="11111111-1111-1111-1111-111111111111",
        )


def test_valid_retry_chain_shape():
    """A well-formed retry: distinct ids for each attempt, root
    points at the original, parent points at the previous
    attempt."""
    original_id = "11111111-1111-1111-1111-111111111111"
    retry_id = "22222222-2222-2222-2222-222222222222"
    r = Run(
        id=retry_id,
        schedule_id="daily_audit",
        execution_plan_hash="a" * 64,
        fire_reason=FireReason.RETRY,
        due_at=_NOW,
        status=RunStatus.PENDING,
        attempt=2,
        root_run_id=original_id,
        parent_run_id=original_id,
    )
    assert r.root_run_id == original_id
    assert r.parent_run_id == original_id
    assert r.id == retry_id


def test_attempt_must_be_positive():
    with pytest.raises(ValidationError):
        Run(**_first_attempt_kwargs(attempt=0))


# ---------------------------------------------------------------------------
# Optional fields default to None
# ---------------------------------------------------------------------------


def test_optional_fields_default_none():
    r = Run(**_first_attempt_kwargs())
    assert r.claimed_by is None
    assert r.claimed_at is None
    assert r.started_at is None
    assert r.completed_at is None
    assert r.error is None


def test_execution_plan_hash_optional():
    """Reminder-shape ScheduleSpecs have no ExecutionPlan; their
    Runs carry None in execution_plan_hash."""
    kwargs = _first_attempt_kwargs()
    kwargs["execution_plan_hash"] = None
    r = Run(**kwargs)
    assert r.execution_plan_hash is None


# ---------------------------------------------------------------------------
# extra-field rejection
# ---------------------------------------------------------------------------


def test_extra_fields_rejected():
    """Catch field-name typos at construction time."""
    with pytest.raises(ValidationError):
        Run(**_first_attempt_kwargs(retries=3))
