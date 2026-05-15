"""V2 scheduler — authoring field setters.

Phase 7 slice 2 per ``docs/PHASE_7_PLAN.md`` §3.3 + §5.3.

Six tools that build a :class:`ScheduleSpecDraft` step by
step:

- :func:`schedule_draft_start` — create a new draft.
- :func:`schedule_set_description` — mutate the description.
- :func:`schedule_set_owner` — set the platform-scoped owner.
- :func:`schedule_set_cron` — set a cron trigger.
- :func:`schedule_set_one_off` — set a one-off trigger.
- :func:`schedule_set_failure_policy` — set the failure
  policy.

Each setter follows the round-2 reviewer Q8 pattern:

1. Load the draft via :meth:`DraftStore.read`.
2. Validate the changed field IMMEDIATELY (per-field
   pydantic + explicit guards for hazards pydantic alone
   misses — round-3 reviewer L381):
   - cron: :func:`reject_numeric_dow` +
     :class:`zoneinfo.ZoneInfo` constructor.
   - one_off: naive-datetime guard +
     :class:`zoneinfo.ZoneInfo`; tz-aware past datetime is
     accepted with a warning-severity issue.
   - owner: platform allowlist
     ``{"slack", "telegram", "email"}``.
3. Apply the field update in memory.
4. If the draft is complete after the mutation: build the
   spec via :meth:`ScheduleSpecDraft.to_spec` and call
   :func:`validate_schedule_spec(spec)`. Failures →
   :meth:`ToolResponse.validation_failed`; the draft on
   disk is NOT updated.
5. If the draft is still incomplete: write the mutated
   draft and return :meth:`ToolResponse.not_ready` with the
   ``missing_fields`` list.

References:
- ``docs/PHASE_7_PLAN.md`` §3.3 + §5.3
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.1 + §5.5
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from app.v2.authoring.drafts import DraftStore, ScheduleSpecDraft
from app.v2.authoring.responses import ToolResponse
from app.v2.enums import FailureActionType, RetryStrategy
from app.v2.models.common import (
    FailurePolicy,
    RetryPolicy,
    UserRef,
)
from app.v2.models.triggers import CronTrigger, OneOffTrigger
from app.v2.runtime.cron_guard import reject_numeric_dow
from app.v2.validation import ValidationIssue, validate_schedule_spec


_ALLOWED_PLATFORMS: frozenset[str] = frozenset(
    {"slack", "telegram", "email"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _draft_not_found(draft_id: str) -> ToolResponse:
    return ToolResponse.not_found(
        message=f"draft {draft_id!r} not found"
    )


def _issue(
    code: str, path: str, message: str, *, severity: str = "error"
) -> ValidationIssue:
    return ValidationIssue(
        code=code, severity=severity, path=path, message=message
    )


def _validation_failed_single(
    code: str, path: str, message: str
) -> ToolResponse:
    return ToolResponse.validation_failed(
        issues=[_issue(code, path, message)]
    )


def _validation_failed_from_pydantic(
    exc: ValidationError, *, base_path: str
) -> ToolResponse:
    issues: list[ValidationIssue] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        issues.append(
            _issue(
                code=str(err.get("type", "validation_error")),
                path=f"{base_path}.{loc}" if loc != "<root>" else base_path,
                message=str(err.get("msg", "")),
            )
        )
    return ToolResponse.validation_failed(issues=issues)


def _finalise(
    draft: ScheduleSpecDraft,
    session_id: str,
    store: DraftStore,
    clock: Callable[[], datetime],
    *,
    warnings: Optional[list[ValidationIssue]] = None,
) -> ToolResponse:
    """Run the post-mutation pipeline.

    - If draft incomplete: write + return :meth:`not_ready`.
    - If draft complete: build spec, call
      :func:`validate_schedule_spec`. On error: do NOT write;
      return :meth:`validation_failed`. On success: write +
      return :meth:`ok`.

    ``warnings`` is the list of warning-severity issues
    collected by the per-field guards (currently only the
    past-OneOff guard). They surface in the success
    response via the ``message`` field as a human-readable
    summary. Validation failures still take precedence.
    """
    missing = draft.missing_required_fields()
    if missing:
        store.write(session_id, draft)
        return ToolResponse.not_ready(missing_fields=missing)

    try:
        spec = draft.to_spec(clock=clock)
    except ValueError as exc:
        # ``to_spec`` raises on naive clock; mirror as
        # validation_failed so the LLM sees a clean shape.
        return _validation_failed_single(
            code="to_spec_failed",
            path="<root>",
            message=str(exc),
        )

    result = validate_schedule_spec(spec)
    if not result.ok:
        return ToolResponse.validation_failed(
            issues=list(result.issues)
        )

    store.write(session_id, draft)
    if warnings:
        message = "; ".join(w.message for w in warnings)
        return ToolResponse.ok(
            draft_id=draft.id, message=message
        )
    return ToolResponse.ok(draft_id=draft.id)


# ---------------------------------------------------------------------------
# schedule_draft_start
# ---------------------------------------------------------------------------


async def schedule_draft_start(
    schedule_id: str,
    description: str,
    *,
    session_id: str,
    owner_platform: str,
    owner_user_id: str,
    owner_display_name: Optional[str] = None,
    store: DraftStore,
    clock: Callable[[], datetime],
) -> ToolResponse:
    """Create a new draft. The draft starts with id /
    description / owner populated; trigger / delivery /
    failure are unset.

    Returns :meth:`ToolResponse.ok(draft_id=schedule_id)` on
    success. Per-field guard failures (e.g. unknown
    platform, description too short) → :meth:`validation_failed`.
    """
    if owner_platform not in _ALLOWED_PLATFORMS:
        return _validation_failed_single(
            code="unknown_platform",
            path="owner.platform",
            message=(
                f"unknown platform {owner_platform!r}; allowed: "
                f"{sorted(_ALLOWED_PLATFORMS)!r}"
            ),
        )

    try:
        owner = UserRef(
            platform=owner_platform,
            user_id=owner_user_id,
            display_name=owner_display_name,
        )
    except ValidationError as exc:
        return _validation_failed_from_pydantic(exc, base_path="owner")

    try:
        draft = ScheduleSpecDraft(
            id=schedule_id,
            description=description,
            owner=owner,
        )
    except ValidationError as exc:
        return _validation_failed_from_pydantic(exc, base_path="<root>")

    return _finalise(draft, session_id, store, clock)


# ---------------------------------------------------------------------------
# schedule_set_description
# ---------------------------------------------------------------------------


async def schedule_set_description(
    draft_id: str,
    description: str,
    *,
    session_id: str,
    store: DraftStore,
    clock: Callable[[], datetime],
) -> ToolResponse:
    try:
        draft = store.read(session_id, draft_id)
    except FileNotFoundError:
        return _draft_not_found(draft_id)

    try:
        new_draft = draft.model_copy(update={"description": description})
    except ValidationError as exc:
        return _validation_failed_from_pydantic(exc, base_path="description")

    return _finalise(new_draft, session_id, store, clock)


# ---------------------------------------------------------------------------
# schedule_set_owner
# ---------------------------------------------------------------------------


async def schedule_set_owner(
    draft_id: str,
    *,
    platform: str,
    user_id: str,
    display_name: Optional[str] = None,
    session_id: str,
    store: DraftStore,
    clock: Callable[[], datetime],
) -> ToolResponse:
    try:
        draft = store.read(session_id, draft_id)
    except FileNotFoundError:
        return _draft_not_found(draft_id)

    if platform not in _ALLOWED_PLATFORMS:
        return _validation_failed_single(
            code="unknown_platform",
            path="owner.platform",
            message=(
                f"unknown platform {platform!r}; allowed: "
                f"{sorted(_ALLOWED_PLATFORMS)!r}"
            ),
        )

    try:
        owner = UserRef(
            platform=platform,
            user_id=user_id,
            display_name=display_name,
        )
    except ValidationError as exc:
        return _validation_failed_from_pydantic(exc, base_path="owner")

    new_draft = draft.model_copy(update={"owner": owner})
    return _finalise(new_draft, session_id, store, clock)


# ---------------------------------------------------------------------------
# schedule_set_cron
# ---------------------------------------------------------------------------


async def schedule_set_cron(
    draft_id: str,
    cron_expr: str,
    timezone_name: str,
    *,
    session_id: str,
    store: DraftStore,
    clock: Callable[[], datetime],
) -> ToolResponse:
    try:
        draft = store.read(session_id, draft_id)
    except FileNotFoundError:
        return _draft_not_found(draft_id)

    # Guard 1: numeric DOW.
    try:
        reject_numeric_dow(cron_expr)
    except ValueError as exc:
        return _validation_failed_single(
            code="numeric_dow_rejected",
            path="trigger.cron",
            message=str(exc),
        )

    # Guard 2: timezone constructor.
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        return _validation_failed_single(
            code="unknown_timezone",
            path="trigger.timezone",
            message=f"ZoneInfoNotFoundError: {exc}",
        )

    # Guard 3: pydantic field validation (5-field count etc.).
    try:
        trigger = CronTrigger(cron=cron_expr, timezone=timezone_name)
    except ValidationError as exc:
        return _validation_failed_from_pydantic(exc, base_path="trigger")

    new_draft = draft.model_copy(update={"trigger": trigger})
    return _finalise(new_draft, session_id, store, clock)


# ---------------------------------------------------------------------------
# schedule_set_one_off
# ---------------------------------------------------------------------------


async def schedule_set_one_off(
    draft_id: str,
    at_iso_datetime: str,
    timezone_name: str,
    *,
    session_id: str,
    store: DraftStore,
    clock: Callable[[], datetime],
) -> ToolResponse:
    try:
        draft = store.read(session_id, draft_id)
    except FileNotFoundError:
        return _draft_not_found(draft_id)

    # Guard 1: parse + naive check.
    try:
        parsed = datetime.fromisoformat(at_iso_datetime)
    except ValueError as exc:
        return _validation_failed_single(
            code="invalid_iso_datetime",
            path="trigger.at_iso_datetime",
            message=f"could not parse ISO 8601 datetime: {exc}",
        )

    if (
        parsed.tzinfo is None
        or parsed.tzinfo.utcoffset(parsed) is None
    ):
        return _validation_failed_single(
            code="naive_datetime",
            path="trigger.at_iso_datetime",
            message=(
                "at_iso_datetime must be tz-aware (include an "
                "explicit timezone offset); got naive datetime"
            ),
        )

    # Guard 2: timezone constructor.
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        return _validation_failed_single(
            code="unknown_timezone",
            path="trigger.timezone",
            message=f"ZoneInfoNotFoundError: {exc}",
        )

    # Build the trigger.
    try:
        trigger = OneOffTrigger(
            at_iso_datetime=parsed,
            timezone=timezone_name,
        )
    except ValidationError as exc:
        return _validation_failed_from_pydantic(exc, base_path="trigger")

    # Past-datetime warning (does NOT block the setter — boot
    # backfill at phase 5 handles past OneOffs; an explicit
    # guard here would prevent legitimate "fire ASAP" intents).
    warnings: list[ValidationIssue] = []
    now_utc = clock().astimezone(timezone.utc)
    if parsed.astimezone(timezone.utc) < now_utc:
        warnings.append(
            _issue(
                code="one_off_in_past",
                path="trigger.at_iso_datetime",
                message=(
                    f"at_iso_datetime {parsed.isoformat()} is "
                    f"before now {now_utc.isoformat()}; the "
                    "boot backfill scan will fire on next "
                    "startup if within max_backfill_age"
                ),
                severity="warning",
            )
        )

    new_draft = draft.model_copy(update={"trigger": trigger})
    return _finalise(
        new_draft, session_id, store, clock, warnings=warnings
    )


# ---------------------------------------------------------------------------
# schedule_set_failure_policy
# ---------------------------------------------------------------------------


async def schedule_set_failure_policy(
    draft_id: str,
    on_failure_action: FailureActionType,
    *,
    retry_strategy: Optional[RetryStrategy] = None,
    retry_base_seconds: Optional[int] = None,
    retry_max_attempts: Optional[int] = None,
    session_id: str,
    store: DraftStore,
    clock: Callable[[], datetime],
) -> ToolResponse:
    try:
        draft = store.read(session_id, draft_id)
    except FileNotFoundError:
        return _draft_not_found(draft_id)

    retry_policy: Optional[RetryPolicy] = None
    if retry_strategy is not None:
        # Both base + max must accompany a strategy choice.
        if retry_base_seconds is None or retry_max_attempts is None:
            return _validation_failed_single(
                code="retry_policy_incomplete",
                path="failure.retry_policy",
                message=(
                    "retry_strategy requires both "
                    "retry_base_seconds and retry_max_attempts"
                ),
            )
        try:
            retry_policy = RetryPolicy(
                strategy=retry_strategy,
                base_seconds=retry_base_seconds,
                max_attempts=retry_max_attempts,
            )
        except ValidationError as exc:
            return _validation_failed_from_pydantic(
                exc, base_path="failure.retry_policy"
            )

    try:
        failure = FailurePolicy(
            on_failure_action=on_failure_action,
            retry_policy=retry_policy,
        )
    except ValidationError as exc:
        return _validation_failed_from_pydantic(exc, base_path="failure")

    new_draft = draft.model_copy(update={"failure": failure})
    return _finalise(new_draft, session_id, store, clock)


__all__ = [
    "schedule_draft_start",
    "schedule_set_cron",
    "schedule_set_description",
    "schedule_set_failure_policy",
    "schedule_set_one_off",
    "schedule_set_owner",
]
