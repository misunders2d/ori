"""Phase 11 slice 6 — RecurringSeriesFromSource builder.

Per ``docs/PHASE_11_PLAN.md`` §3.3 / §5 + the
claude-reviewer slice-6 hard-checks: the builder produces
a valid frozen ``(ScheduleSpec, ExecutionPlan)`` —
exactly ONE source input, ONE ``source_post`` emit, ZERO
reasoning steps; typed args; bad slots rejected; STATELESS
(no new state table — Q2).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.v2.enums import LiveChangePolicy, SourceFallbackPolicy
from app.v2.models.common import LiveSourceCachePolicy, UserRef
from app.v2.models.source_ref import SourceRefSpec
from app.v2.models.triggers import CronTrigger
from app.v2.templates.recurring_series_from_source import (
    RECURRING_SERIES_FROM_SOURCE_TEMPLATE_NAME,
    RECURRING_SERIES_FROM_SOURCE_TEMPLATE_VERSION,
    RecurringSeriesFromSourceArgs,
    build_recurring_series_from_source,
)

_T0 = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


def _owner() -> UserRef:
    return UserRef(
        platform="slack", user_id="U_OWN", display_name="S"
    )


def _source() -> SourceRefSpec:
    return SourceRefSpec(
        loader="source_literal",
        args={"source_id": "src", "text": "hello"},
        cache=LiveSourceCachePolicy(
            cache_ttl_seconds=300,
            stale_max_age_seconds=3600,
            fallback_policy=SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT,
        ),
        live_change_policy=LiveChangePolicy.ALLOW,
    )


def _build(**over):
    kw = dict(
        source=_source(),
        channel="C012ABCDE",
        hour_local=9,
        timezone_name="Europe/Kyiv",
        progress_strategy="whole",
        owner=_owner(),
        schedule_id="sched_rsfs",
        clock=lambda: _T0,
    )
    kw.update(over)
    return build_recurring_series_from_source(**kw)


# ---------------------------------------------------------------------------
# Happy path — valid frozen (ScheduleSpec, ExecutionPlan)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["whole", "skip_unchanged"])
def test_builds_valid_spec_and_plan(strategy):
    spec, plan = _build(progress_strategy=strategy)

    # ExecutionPlan: ONE source input, ZERO reasoning, ONE
    # source_post emit carrying channel + progress_strategy.
    assert len(plan.inputs) == 1
    inp = plan.inputs[0]
    assert inp.source_ref is not None
    assert inp.loader == "source_literal"
    assert plan.reasoning == []
    assert len(plan.emit) == 1
    em = plan.emit[0]
    assert em.adapter == "source_post"
    assert em.args == {
        "channel": "C012ABCDE",
        "progress_strategy": strategy,
    }
    assert plan.hash  # frozen

    # ScheduleSpec: daily cron → the plan; template echo.
    assert spec.hash  # frozen
    assert spec.execution_plan_hash == plan.hash
    assert isinstance(spec.trigger, CronTrigger)
    assert spec.trigger.cron == "0 9 * * *"
    assert spec.trigger.timezone == "Europe/Kyiv"
    assert spec.template is not None
    assert (
        spec.template.name
        == RECURRING_SERIES_FROM_SOURCE_TEMPLATE_NAME
    )
    assert (
        spec.template.version
        == RECURRING_SERIES_FROM_SOURCE_TEMPLATE_VERSION
    )
    assert spec.template.args == RecurringSeriesFromSourceArgs(
        source_loader="source_literal",
        channel="C012ABCDE",
        hour_local=9,
        timezone="Europe/Kyiv",
        progress_strategy=strategy,
    ).model_dump()
    # execution_plan_hash is set ⇒ NOT a OneOff-style spec.
    assert spec.execution_plan_hash is not None


def test_distinct_source_rehashes_the_plan():
    _, p1 = _build()
    other = _source()
    other = other.model_copy(
        update={"args": {"source_id": "src", "text": "DIFFERENT"}}
    )
    _, p2 = _build(source=other)
    assert p1.hash != p2.hash  # body change ⇒ new hash


# ---------------------------------------------------------------------------
# Bad-slot rejection (typed args / CronTrigger validators)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hour", [-1, 24, 99])
def test_hour_out_of_range_rejected(hour):
    with pytest.raises(ValidationError):
        _build(hour_local=hour)


def test_empty_channel_rejected():
    with pytest.raises(ValidationError):
        _build(channel="")


def test_empty_timezone_rejected():
    with pytest.raises(ValidationError):
        _build(timezone_name="")


def test_unknown_progress_strategy_rejected():
    with pytest.raises(ValidationError):
        _build(progress_strategy="every_other_tuesday")


def test_naive_clock_rejected_with_value_error():
    with pytest.raises(ValueError, match="naive datetime"):
        _build(clock=lambda: datetime(2026, 5, 16, 12, 0))


# ---------------------------------------------------------------------------
# Typed args model
# ---------------------------------------------------------------------------


def test_args_model_defaults_and_forbids_extra():
    a = RecurringSeriesFromSourceArgs(
        source_loader="source_literal",
        channel="C1",
        hour_local=0,
        timezone="UTC",
    )
    assert a.progress_strategy == "whole"  # default
    with pytest.raises(ValidationError):
        RecurringSeriesFromSourceArgs(
            source_loader="x",
            channel="C1",
            hour_local=0,
            timezone="UTC",
            bogus=1,
        )
