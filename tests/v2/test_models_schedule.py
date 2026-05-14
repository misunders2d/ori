"""Tests for ``app.v2.models.schedule``.

Pins:
- ScheduleSpec ID pattern (snake_case, must start with letter).
- Description minimum length.
- ``status`` defaults to ``active``.
- ``execution_plan_hash is None`` is valid (reminder shape).
- Hash determinism: re-serialise + recompute = same hash.
- Hash excludes ``hash`` and ``authored_at`` so idempotent
  author runs produce stable hashes.
- Parent-hash chain validation (string-only check phase 1;
  cycle detection lands at phase 4 storage layer).
- Tightened Delivery / RetryPolicy enums reject invalid string
  values (round-7 follow-up).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0
- ``docs/PHASE_1_PLAN.md`` §4.4 / §5.1
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
    OnOversizePolicy,
    RetryStrategy,
    ScheduleStatus,
)
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    RetryPolicy,
    UserRef,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import CronTrigger, OneOffTrigger


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _baseline_kwargs(**overrides):
    base = dict(
        id="daily_linux_lesson",
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="Daily Linux command lesson.",
        trigger=CronTrigger(cron="0 20 * * *", timezone="Europe/Kyiv"),
        delivery=Delivery(
            target_session_id="sl_C0B2LJRS8D8",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN
        ),
        audit=AuditPolicy(),
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# id pattern
# ---------------------------------------------------------------------------


def test_id_accepts_snake_case():
    spec = ScheduleSpec(**_baseline_kwargs(id="daily_news_digest"))
    assert spec.id == "daily_news_digest"


def test_id_accepts_single_lowercase_letter():
    spec = ScheduleSpec(**_baseline_kwargs(id="a"))
    assert spec.id == "a"


def test_id_rejects_uppercase():
    with pytest.raises(ValidationError, match="must match"):
        ScheduleSpec(**_baseline_kwargs(id="DailyNews"))


def test_id_rejects_starting_with_digit():
    with pytest.raises(ValidationError):
        ScheduleSpec(**_baseline_kwargs(id="1daily"))


def test_id_rejects_starting_with_underscore():
    with pytest.raises(ValidationError):
        ScheduleSpec(**_baseline_kwargs(id="_daily"))


def test_id_rejects_hyphen():
    with pytest.raises(ValidationError):
        ScheduleSpec(**_baseline_kwargs(id="daily-lesson"))


def test_id_rejects_space():
    with pytest.raises(ValidationError):
        ScheduleSpec(**_baseline_kwargs(id="daily lesson"))


def test_id_rejects_empty():
    with pytest.raises(ValidationError):
        ScheduleSpec(**_baseline_kwargs(id=""))


# ---------------------------------------------------------------------------
# description
# ---------------------------------------------------------------------------


def test_description_min_length_accepted():
    spec = ScheduleSpec(**_baseline_kwargs(description="12345678"))
    assert spec.description == "12345678"


def test_description_below_min_rejected():
    with pytest.raises(ValidationError, match="at least"):
        ScheduleSpec(**_baseline_kwargs(description="short"))


def test_description_whitespace_only_below_min_rejected():
    """Whitespace doesn't count toward the minimum."""
    with pytest.raises(ValidationError, match="at least"):
        ScheduleSpec(**_baseline_kwargs(description="   abc   "))


# ---------------------------------------------------------------------------
# status default + values
# ---------------------------------------------------------------------------


def test_status_defaults_to_active():
    spec = ScheduleSpec(**_baseline_kwargs())
    assert spec.status == ScheduleStatus.ACTIVE


def test_status_accepts_paused():
    spec = ScheduleSpec(**_baseline_kwargs(status=ScheduleStatus.PAUSED))
    assert spec.status == ScheduleStatus.PAUSED


def test_status_accepts_archived():
    spec = ScheduleSpec(**_baseline_kwargs(status=ScheduleStatus.ARCHIVED))
    assert spec.status == ScheduleStatus.ARCHIVED


def test_status_rejects_unknown_value():
    with pytest.raises(ValidationError):
        ScheduleSpec(**_baseline_kwargs(status="hibernating"))


# ---------------------------------------------------------------------------
# execution_plan_hash optionality (reminder shape)
# ---------------------------------------------------------------------------


def test_execution_plan_hash_defaults_none():
    """Reminder shape: no ExecutionPlan needed. None must be the
    default so simple use cases don't have to invent placeholders."""
    spec = ScheduleSpec(**_baseline_kwargs())
    assert spec.execution_plan_hash is None


def test_execution_plan_hash_accepts_sha256_string():
    spec = ScheduleSpec(
        **_baseline_kwargs(execution_plan_hash="a" * 64)
    )
    assert spec.execution_plan_hash == "a" * 64


# ---------------------------------------------------------------------------
# Hash determinism
# ---------------------------------------------------------------------------


def test_compute_hash_is_deterministic():
    spec = ScheduleSpec(**_baseline_kwargs())
    h1 = spec.compute_hash()
    h2 = spec.compute_hash()
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_compute_hash_excludes_authored_at():
    """Two specs identical except for authored_at must hash the
    same. Otherwise idempotent author runs would produce
    different hashes on every invocation."""
    a = ScheduleSpec(**_baseline_kwargs(authored_at="2026-05-14T12:00:00Z"))
    b = ScheduleSpec(**_baseline_kwargs(authored_at="2026-06-01T09:00:00Z"))
    assert a.compute_hash() == b.compute_hash()


def test_compute_hash_excludes_hash_field():
    """Hash should not feed itself."""
    a = ScheduleSpec(**_baseline_kwargs())
    b = ScheduleSpec(**_baseline_kwargs(hash="cafebabe" * 8))
    assert a.compute_hash() == b.compute_hash()


def test_compute_hash_changes_when_body_changes():
    a = ScheduleSpec(**_baseline_kwargs())
    b = ScheduleSpec(
        **_baseline_kwargs(description="Tomorrow's Linux command.")
    )
    assert a.compute_hash() != b.compute_hash()


def test_with_fresh_hash_populates_hash_field():
    spec = ScheduleSpec(**_baseline_kwargs())
    assert spec.hash == ""
    refreshed = spec.with_fresh_hash()
    assert refreshed.hash == spec.compute_hash()
    # Original instance is unchanged (immutable copy semantics).
    assert spec.hash == ""


# ---------------------------------------------------------------------------
# Delivery / RetryPolicy strictness (review round-7 followup)
# ---------------------------------------------------------------------------


def test_delivery_fallback_policy_enum_required():
    """A free-form string is rejected. The whole point of the
    enum tightening was to stop arbitrary strings from leaking
    into Delivery."""
    with pytest.raises(ValidationError):
        Delivery(
            target_session_id="sl_X",
            fallback_policy="ignore_and_move_on",
        )


def test_delivery_fallback_policy_accepts_admin_alert_only():
    d = Delivery(
        target_session_id="sl_X",
        fallback_policy=DeliveryFallbackPolicy.ADMIN_ALERT_ONLY,
    )
    assert d.fallback_policy == DeliveryFallbackPolicy.ADMIN_ALERT_ONLY


def test_retry_policy_strategy_enum_required():
    with pytest.raises(ValidationError):
        RetryPolicy(strategy="linear", base_seconds=10, max_attempts=3)


def test_retry_policy_accepts_exponential():
    p = RetryPolicy(
        strategy=RetryStrategy.EXPONENTIAL, base_seconds=10, max_attempts=5
    )
    assert p.strategy == RetryStrategy.EXPONENTIAL


def test_retry_policy_accepts_fixed():
    p = RetryPolicy(
        strategy=RetryStrategy.FIXED, base_seconds=60, max_attempts=2
    )
    assert p.strategy == RetryStrategy.FIXED


def test_retry_policy_rejects_non_positive_base():
    with pytest.raises(ValidationError):
        RetryPolicy(
            strategy=RetryStrategy.EXPONENTIAL,
            base_seconds=0,
            max_attempts=3,
        )


def test_retry_policy_rejects_non_positive_max_attempts():
    with pytest.raises(ValidationError):
        RetryPolicy(
            strategy=RetryStrategy.FIXED,
            base_seconds=10,
            max_attempts=0,
        )


# ---------------------------------------------------------------------------
# AuditPolicy default surface
# ---------------------------------------------------------------------------


def test_audit_policy_defaults():
    """Phase-1 default values for AuditPolicy match the design
    contract §4.6.4 retention guidance. Authors can override."""
    p = AuditPolicy()
    assert p.keep_last_n_snapshots == 30
    assert p.dedup_by_content_hash is True
    assert p.redact_fields == []
    assert p.max_snapshot_bytes == 1_000_000
    assert p.on_oversize == OnOversizePolicy.FAIL_AND_ALERT


def test_audit_policy_on_oversize_must_be_enum():
    with pytest.raises(ValidationError):
        AuditPolicy(on_oversize="truncate_silently")


# ---------------------------------------------------------------------------
# extra-field rejection
# ---------------------------------------------------------------------------


def test_schedule_spec_forbids_extra_fields():
    """ConfigDict(extra='forbid') everywhere. Extra fields catch
    typos at construction time, not at fire time."""
    with pytest.raises(ValidationError):
        ScheduleSpec(**_baseline_kwargs(retries=3))


# ---------------------------------------------------------------------------
# One-off ScheduleSpec (the lightweight reminder shape)
# ---------------------------------------------------------------------------


def test_one_off_reminder_shape():
    """The reminder shape: one_off trigger, no execution_plan_hash,
    no template. The most common simple-task shape."""
    spec = ScheduleSpec(
        **_baseline_kwargs(
            id="ping_at_4pm",
            trigger=OneOffTrigger(
                at_iso_datetime="2026-05-15T16:00:00+03:00",
                timezone="Europe/Kyiv",
            ),
        )
    )
    assert spec.execution_plan_hash is None
    assert spec.template is None
    assert spec.trigger.type == "one_off"
