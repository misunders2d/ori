"""Tests for ``app.v2.templates.one_off_reminder``.

Phase 9 slice 2 per ``docs/PHASE_9_PLAN.md`` §5.1.

Pins:
- Happy path returns a frozen ScheduleSpec with the
  documented template / trigger / delivery / args shape.
- Delivery target equals ``recipient.external_id``
  (round-1 reviewer L98 pin).
- Naive ``at`` → ValueError before pydantic.
- Non-UTC ``at`` normalised via astimezone(utc).
- Naive clock → ValueError on authored_at.
- Empty ``text`` → pydantic.ValidationError via
  OneOffReminderArgs.
- Oversize ``text`` (4001 chars) → ValidationError.
- ``OneOffReminderArgs.extra="forbid"`` carry-forward.
- AST pin: builder body calls no
  ``datetime.now`` / ``uuid.uuid4``.
- Module hygiene: no ``slack_sdk`` / ``googleapiclient``
  imports at module load; no module-level ``prod_clock``
  bind.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.v2.enums import FailureActionType
from app.v2.models.common import (
    AuditPolicy,
    ChannelRef,
    FailurePolicy,
    TemplateRef,
    UserRef,
)
from app.v2.templates import one_off_reminder as template_mod
from app.v2.templates.one_off_reminder import (
    ONE_OFF_REMINDER_TEMPLATE_NAME,
    ONE_OFF_REMINDER_TEMPLATE_VERSION,
    OneOffReminderArgs,
    build_one_off_reminder,
)


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
_FIRE_AT = _UTC_NOW + timedelta(hours=1)


def _fixed_clock() -> datetime:
    return _UTC_NOW


def _recipient() -> ChannelRef:
    return ChannelRef(kind="slack", external_id="C012ABCDE")


def _owner() -> UserRef:
    return UserRef(
        platform="slack",
        user_id="U_OWNER",
        display_name="Sergey",
    )


# ===========================================================================
# Identity constants
# ===========================================================================


def test_template_name_constant():
    assert ONE_OFF_REMINDER_TEMPLATE_NAME == "OneOffReminder"


def test_template_version_constant():
    assert ONE_OFF_REMINDER_TEMPLATE_VERSION == "1"


# ===========================================================================
# OneOffReminderArgs
# ===========================================================================


def test_args_accepts_text_within_limits():
    args = OneOffReminderArgs(text="weekly digest reminder")
    assert args.text == "weekly digest reminder"


def test_args_rejects_empty_text():
    with pytest.raises(ValidationError):
        OneOffReminderArgs(text="")


def test_args_rejects_oversize_text():
    too_long = "x" * 4001
    with pytest.raises(ValidationError):
        OneOffReminderArgs(text=too_long)


def test_args_accepts_boundary_text():
    """Exactly 4000 chars is accepted; 4001 is not."""
    OneOffReminderArgs(text="x" * 4000)  # no raise


def test_args_forbids_extra_fields():
    with pytest.raises(ValidationError):
        OneOffReminderArgs(
            text="hi",
            snoozable=False,  # type: ignore[call-arg]
        )


# ===========================================================================
# build_one_off_reminder — happy path
# ===========================================================================


def test_happy_path_returns_frozen_spec():
    spec = build_one_off_reminder(
        at=_FIRE_AT,
        recipient=_recipient(),
        text="ping the channel",
        owner=_owner(),
        schedule_id="sched_alpha",
        clock=_fixed_clock,
    )

    assert spec.id == "sched_alpha"
    assert spec.hash  # populated by with_fresh_hash
    assert spec.trigger.type == "one_off"
    assert spec.execution_plan_hash is None
    assert spec.template == TemplateRef(
        name="OneOffReminder",
        version="1",
        args={"text": "ping the channel"},
    )


def test_delivery_target_uses_external_id():
    """Round-1 reviewer L98 pin: delivery target must be
    ``recipient.external_id`` (the field name on phase-1
    ChannelRef), NOT a non-existent ``.id`` attribute."""
    recipient = ChannelRef(kind="slack", external_id="C012ABCDE")
    spec = build_one_off_reminder(
        at=_FIRE_AT,
        recipient=recipient,
        text="hi",
        owner=_owner(),
        schedule_id="sched_alpha",
        clock=_fixed_clock,
    )
    assert spec.delivery.target_session_id == "C012ABCDE"


def test_authored_at_carries_clock_value():
    spec = build_one_off_reminder(
        at=_FIRE_AT,
        recipient=_recipient(),
        text="hi",
        owner=_owner(),
        schedule_id="sched_alpha",
        clock=_fixed_clock,
    )
    assert spec.authored_at.startswith("2026-05-15T12:00:00")
    assert spec.authored_at.endswith("+00:00")


def test_defaults_audit_and_failure_policy():
    spec = build_one_off_reminder(
        at=_FIRE_AT,
        recipient=_recipient(),
        text="hi",
        owner=_owner(),
        schedule_id="sched_alpha",
        clock=_fixed_clock,
    )
    assert spec.audit == AuditPolicy()
    assert (
        spec.failure.on_failure_action == FailureActionType.ALERT_ADMIN
    )


def test_explicit_audit_and_failure_passed_through():
    custom_audit = AuditPolicy()
    custom_failure = FailurePolicy(
        on_failure_action=FailureActionType.ABORT_SILENT
    )
    spec = build_one_off_reminder(
        at=_FIRE_AT,
        recipient=_recipient(),
        text="hi",
        owner=_owner(),
        schedule_id="sched_alpha",
        audit=custom_audit,
        failure=custom_failure,
        clock=_fixed_clock,
    )
    assert spec.audit is custom_audit
    assert spec.failure is custom_failure


# ===========================================================================
# build_one_off_reminder — failure modes
# ===========================================================================


def test_naive_at_raises_value_error():
    naive = datetime(2026, 5, 15, 13, 0)  # no tzinfo
    with pytest.raises(ValueError, match="tz-aware UTC"):
        build_one_off_reminder(
            at=naive,
            recipient=_recipient(),
            text="hi",
            owner=_owner(),
            schedule_id="sched_alpha",
            clock=_fixed_clock,
        )


def test_non_utc_at_normalised_to_utc():
    """tz-aware non-UTC ``at`` normalised via
    astimezone(timezone.utc); trigger carries the UTC
    wall clock."""
    five_east = timezone(timedelta(hours=5))
    at_east = datetime(2026, 5, 15, 12, 0, tzinfo=five_east)
    # 12:00 +05 == 07:00 UTC.

    spec = build_one_off_reminder(
        at=at_east,
        recipient=_recipient(),
        text="hi",
        owner=_owner(),
        schedule_id="sched_alpha",
        clock=_fixed_clock,
    )
    # OneOffTrigger.at_iso_datetime is the normalised value.
    at_iso = spec.trigger.at_iso_datetime
    if isinstance(at_iso, datetime):
        assert at_iso == datetime(
            2026, 5, 15, 7, 0, tzinfo=timezone.utc
        )
    else:
        # If the trigger stores as ISO string, ensure UTC offset.
        assert "07:00:00" in at_iso
        assert at_iso.endswith("+00:00")


def test_naive_clock_raises_value_error():
    def _naive_clock() -> datetime:
        return datetime(2026, 5, 15, 12, 0)  # no tzinfo

    with pytest.raises(ValueError, match="tz-aware UTC"):
        build_one_off_reminder(
            at=_FIRE_AT,
            recipient=_recipient(),
            text="hi",
            owner=_owner(),
            schedule_id="sched_alpha",
            clock=_naive_clock,
        )


def test_empty_text_raises_validation_error():
    with pytest.raises(ValidationError):
        build_one_off_reminder(
            at=_FIRE_AT,
            recipient=_recipient(),
            text="",
            owner=_owner(),
            schedule_id="sched_alpha",
            clock=_fixed_clock,
        )


def test_oversize_text_raises_validation_error():
    with pytest.raises(ValidationError):
        build_one_off_reminder(
            at=_FIRE_AT,
            recipient=_recipient(),
            text="x" * 4001,
            owner=_owner(),
            schedule_id="sched_alpha",
            clock=_fixed_clock,
        )


# ===========================================================================
# Module hygiene
# ===========================================================================


def test_module_does_not_bind_prod_clock():
    """Phase-5 rule 10: only ``_defaults.py`` may bind a
    runtime clock. The template module stays clock-pure."""
    assert not hasattr(template_mod, "prod_clock")


def test_module_does_not_bind_uuid4_factory():
    src = inspect.getsource(template_mod)
    assert "uuid4" not in src


def test_module_imports_no_slack_sdk():
    src = inspect.getsource(template_mod)
    assert "slack_sdk" not in src


def test_module_imports_no_googleapiclient():
    src = inspect.getsource(template_mod)
    assert "googleapiclient" not in src


def test_build_function_body_calls_no_datetime_now():
    """AST pin: the builder calls no
    ``datetime.now`` / ``datetime.utcnow``."""
    source = textwrap.dedent(inspect.getsource(build_one_off_reminder))
    tree = ast.parse(source)
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            parts: list[str] = []
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                parts.append(target.id)
                calls.add(".".join(reversed(parts)))

    forbidden = {"datetime.now", "datetime.utcnow"}
    leaked = calls & forbidden
    assert not leaked, (
        f"build_one_off_reminder must be clock-free; got {leaked!r}"
    )
