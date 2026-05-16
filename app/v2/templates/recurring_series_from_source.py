"""V2 scheduler — RecurringSeriesFromSource template.

Phase 11 slice 6 per ``docs/PHASE_11_PLAN.md`` §3.3 / §4
slice 6 + the claude-reviewer slice-6 hard-checks.

A recurring (daily-cron) source-driven schedule: each
fire resolves ONE source and posts it VERBATIM to a Slack
channel. STATELESS progress strategies only (Q2 — NO new
state table):

- ``whole`` — post the full resolved content every fire.
- ``skip_unchanged`` — post UNLESS the resolved content
  is unchanged vs the immediately-prior materialised
  snapshot. The "unchanged" decision is made by the
  resolver/emit path via the slice-4 ADDITIVE
  ``ResolveOutcome.changed_vs_prior`` signal — the worker
  NEVER re-reads the snapshot table (the original 🔴
  invariant). A skip is a no-op SUCCESS recorded on the
  EXISTING ``RUN_SUCCEEDED`` via the typed
  ``RunSucceededPayload.skipped_unchanged`` discriminator
  (Option B — no new event kind, no v002 schema
  migration; ``emit_skipped_idempotent`` stays RESERVED
  for step-14 idempotency dedup, NOT content-unchanged).

Surface (mirrors :mod:`app.v2.templates.one_off_reminder`):

- :data:`RECURRING_SERIES_FROM_SOURCE_TEMPLATE_NAME` /
  :data:`RECURRING_SERIES_FROM_SOURCE_TEMPLATE_VERSION`.
- :class:`RecurringSeriesFromSourceArgs` — typed
  ``TemplateRef.args`` model.
- :func:`build_recurring_series_from_source` — pure
  builder returning a frozen ``(ScheduleSpec,
  ExecutionPlan)``: ExecutionPlan with exactly ONE source
  input + ONE ``source_post`` emit + ZERO reasoning steps;
  ScheduleSpec with a daily ``CronTrigger`` pointing at
  the plan via ``execution_plan_hash``.

No vendor SDK at module load; ``clock()`` is the sole
authority for ``authored_at`` (phase-9/10 clock-pure
hard rule — AST-pinned).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.2, §5.3
- ``docs/PHASE_11_PLAN.md`` §3.3
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.v2.enums import DeliveryFallbackPolicy, FailureActionType
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    TemplateRef,
    UserRef,
)
from app.v2.models.execution_plan import (
    EmitStep,
    ExecutionPlan,
    InputSpec,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.source_ref import SourceRefSpec
from app.v2.models.triggers import CronTrigger

RECURRING_SERIES_FROM_SOURCE_TEMPLATE_NAME = (
    "RecurringSeriesFromSource"
)
RECURRING_SERIES_FROM_SOURCE_TEMPLATE_VERSION = "1"

#: The registered emit-adapter name (must match
#: ``app.v2.emit.source_post.SOURCE_POST_ADAPTER``; kept a
#: literal here so this builder has NO runtime import of
#: the emit module / its Slack Protocol).
_SOURCE_POST_ADAPTER = "source_post"

#: STATELESS progress strategies only (Q2). The stateful
#: "next unread item" strategy defers to 11B (step-13
#: cross-fire schedule_state).
ProgressStrategy = Literal["whole", "skip_unchanged"]


class RecurringSeriesFromSourceArgs(BaseModel):
    """Typed payload carried on ``TemplateRef.args``.

    The human-facing slots — echoed so a body change
    re-hashes the ScheduleSpec and the authoring tool
    (slice 7) can render the schedule back. The
    authoritative source policy lives in the ExecutionPlan
    body (``InputSpec.source_ref``, hashed via
    ``execution_plan_hash``); these are the slot echo.
    """

    model_config = ConfigDict(extra="forbid")

    source_loader: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    hour_local: int = Field(ge=0, le=23)
    timezone: str = Field(min_length=1)
    progress_strategy: ProgressStrategy = "whole"


def build_recurring_series_from_source(
    *,
    source: SourceRefSpec,
    channel: str,
    hour_local: int,
    timezone_name: str,
    progress_strategy: ProgressStrategy,
    owner: UserRef,
    schedule_id: str,
    audit: AuditPolicy | None = None,
    failure: FailurePolicy | None = None,
    clock: Callable[[], datetime],
) -> tuple[ScheduleSpec, ExecutionPlan]:
    """Build a frozen ``(ScheduleSpec, ExecutionPlan)`` for
    a recurring source-driven series.

    Args:
        source: The validated :class:`SourceRefSpec`
            (loader + args + cache + live-change + default)
            for the single series source.
        channel: Slack channel id the resolved content is
            posted to (carried on the ``source_post``
            EmitStep args — NOT ``spec.delivery``).
        hour_local: Daily fire hour 0-23 (interpreted in
            ``timezone_name``). Out-of-range → a
            ``pydantic.ValidationError`` from
            :class:`RecurringSeriesFromSourceArgs` BEFORE
            any spec construction (the "bad slot" guard;
            there is no datetime slot — the trigger is a
            5-field daily cron).
        timezone_name: IANA tz name. Empty → rejected by
            the args model AND the :class:`CronTrigger`
            validator.
        progress_strategy: ``"whole"`` | ``"skip_unchanged"``
            (STATELESS — Q2). Anything else → typed
            ``ValidationError``.
        owner: Author / owner identity.
        schedule_id: Caller-generated stable id (typically
            from the authoring tool's
            ``schedule_id_factory``).
        audit / failure: Defaults to ``AuditPolicy()`` /
            ``alert_admin``.
        clock: Tz-aware UTC clock — sole authority for
            ``authored_at``. Naive output → ``ValueError``.

    Returns:
        ``(ScheduleSpec, ExecutionPlan)``, both frozen
        (``.with_fresh_hash()``); the ScheduleSpec's
        ``execution_plan_hash`` is the plan's hash. The
        ExecutionPlan has exactly ONE source input, ONE
        ``source_post`` emit, ZERO reasoning steps.

    Raises:
        ValueError: ``clock()`` returned a naive datetime.
        pydantic.ValidationError: a bad slot (hour range /
            empty channel / empty tz / unknown
            progress_strategy) or an invalid cron.
    """
    # ---- 1. Slot validation (typed; rejects bad slots) ----
    args = RecurringSeriesFromSourceArgs(
        source_loader=source.loader,
        channel=channel,
        hour_local=hour_local,
        timezone=timezone_name,
        progress_strategy=progress_strategy,
    )

    # ---- 2. Defaults ----
    if audit is None:
        audit = AuditPolicy()
    if failure is None:
        failure = FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN
        )

    # ---- 3. clock() tz-aware enforcement ----
    clock_value = clock()
    if (
        clock_value.tzinfo is None
        or clock_value.tzinfo.utcoffset(clock_value) is None
    ):
        raise ValueError(
            "build_recurring_series_from_source: clock() "
            "returned a naive datetime; authored_at must be "
            "tz-aware UTC"
        )
    authored_at = clock_value.astimezone(
        timezone.utc
    ).isoformat()

    # ---- 4. ExecutionPlan: 1 source input, 1 source_post
    # emit, 0 reasoning ----
    plan = ExecutionPlan(
        id="recurring_series_from_source",
        description=(
            f"RecurringSeriesFromSource: {source.loader} "
            f"-> {channel}"
        ),
        author=owner.user_id,
        inputs=[
            InputSpec(
                id="src",
                loader=source.loader,
                source_ref=source,
            )
        ],
        reasoning=[],
        emit=[
            EmitStep(
                id="post",
                adapter=_SOURCE_POST_ADAPTER,
                args={
                    "channel": channel,
                    "progress_strategy": progress_strategy,
                },
            )
        ],
    ).with_fresh_hash()

    # ---- 5. ScheduleSpec: daily cron → the plan ----
    spec = ScheduleSpec(
        id=schedule_id,
        description=(
            f"RecurringSeriesFromSource: {source.loader} "
            f"-> {channel} @ {hour_local:02d}:00 "
            f"{timezone_name}"
        ),
        owner=owner,
        trigger=CronTrigger(
            cron=f"0 {hour_local} * * *",
            timezone=timezone_name,
        ),
        delivery=Delivery(
            target_session_id=channel,
            fallback_policy=(
                DeliveryFallbackPolicy.SESSION_TO_ORIGIN
            ),
        ),
        failure=failure,
        audit=audit,
        execution_plan_hash=plan.hash,
        template=TemplateRef(
            name=RECURRING_SERIES_FROM_SOURCE_TEMPLATE_NAME,
            version=RECURRING_SERIES_FROM_SOURCE_TEMPLATE_VERSION,
            args=args.model_dump(),
        ),
        parent_hash=None,
        authored_at=authored_at,
    ).with_fresh_hash()

    return spec, plan


__all__ = [
    "RECURRING_SERIES_FROM_SOURCE_TEMPLATE_NAME",
    "RECURRING_SERIES_FROM_SOURCE_TEMPLATE_VERSION",
    "ProgressStrategy",
    "RecurringSeriesFromSourceArgs",
    "build_recurring_series_from_source",
]
