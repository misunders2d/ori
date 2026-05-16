"""V2 scheduler — template-driven authoring tools.

Phase 9 slice 3 per ``docs/PHASE_9_PLAN.md`` §1.3 + §3.3.

Ships the first end-to-end template authoring tool:
``schedule_create_reminder``. The agent calls a closure
that exposes ONLY ``(at, recipient_channel, text)``; every
DI dependency (store / handshake_store / conn factory /
cache loader+saver / slack client / clock / id factories
/ owner) is captured at startup via the
:func:`make_schedule_create_reminder` factory.

Pipeline (closure body):

1. Parse ``at`` from an ISO 8601 string. Naive →
   ``validation_failed(naive_at_datetime)``; non-ISO →
   ``validation_failed(invalid_iso_datetime)``.
2. Resolve ``recipient_channel`` via the phase-6 cache
   resolver. Cache absent + refresh fails →
   ``cache_unavailable``. Cache present + channel name
   not found → ``not_found``. Multiple matches →
   ``validation_failed(channel_ambiguous)``.
3. Build the spec via :func:`build_one_off_reminder`.
4. Persist the spec as a draft via
   :meth:`DraftStore.write`.
5. Run :func:`schedule_dry_run(VALIDATE_ONLY)` to record
   the handshake.
6. Run :func:`schedule_freeze` against a fresh DB
   connection.
7. Run :func:`schedule_draft_commit` against the same
   connection; surfaces ``ok(schedule_id, spec)`` on
   success or forwards the verb's failure shape.

DI design (round-2 reviewer L500 fix): the factory
returns an async function whose LLM-visible signature is
EXACTLY ``(at, recipient_channel, text)``. ADK
``FunctionTool`` introspects that signature when building
the schema sent to the model; the production wrapper
factory pattern keeps every DI parameter out of the
schema. The slice's tests pin this introspection.

References:
- ``docs/PHASE_9_PLAN.md`` §1.3 + §3.3
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.5
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Awaitable, Callable, Optional

from pydantic import ValidationError

from app.v2.authoring.commit import schedule_draft_commit
from app.v2.authoring.drafts import (
    DraftStore,
    ScheduleSpecDraft,
)
from app.v2.authoring.dry_run import schedule_dry_run
from app.v2.authoring.freeze import schedule_freeze
from app.v2.authoring.handshake import (
    DryRunMode,
    HandshakeStore,
)
from app.v2.authoring.responses import ToolResponse
from app.v2.authoring.setters import _validation_failed_single
from app.v2.models.common import UserRef
from app.v2.registry_cache.errors import (
    CacheMiss,
    ChannelAmbiguous,
    NoCacheAvailable,
)
from app.v2.registry_cache.refresh import (
    SlackChannelsClient,
    refresh_slack_channels,
)
from app.v2.registry_cache.resolver import resolve_channel
from app.v2.registry_cache.schemas import SlackChannelsCache
from app.v2.enums import LiveChangePolicy, SourceFallbackPolicy
from app.v2.models.common import LiveSourceCachePolicy
from app.v2.models.source_ref import SourceRefSpec
from app.v2.registry import SOURCES
from app.v2.templates.one_off_reminder import build_one_off_reminder
from app.v2.templates.recurring_series_from_source import (
    build_recurring_series_from_source,
)
from app.v2.validation import ValidationIssue


SCHEDULE_CREATE_REMINDER_TOOL_NAME = "schedule_create_reminder"


def _parse_iso_datetime(value: str) -> tuple[Optional[datetime], Optional[ToolResponse]]:
    """Parse an ISO 8601 datetime string. Returns
    ``(parsed, None)`` on success and ``(None,
    validation_failed(...))`` on failure.

    Naive datetimes surface as ``naive_at_datetime``;
    non-ISO strings surface as ``invalid_iso_datetime``.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        return None, _validation_failed_single(
            code="invalid_iso_datetime",
            path="at",
            message=(
                f"could not parse {value!r} as an ISO 8601 "
                f"datetime: {exc!s}"
            ),
        )
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None, _validation_failed_single(
            code="naive_at_datetime",
            path="at",
            message=(
                f"`at` {value!r} is naive; expected an "
                "ISO 8601 datetime carrying a UTC offset "
                "(e.g. '2026-05-15T16:00:00+00:00')"
            ),
        )
    return parsed, None


def make_schedule_create_reminder(
    *,
    store: DraftStore,
    handshake_store: HandshakeStore,
    conn_factory: Callable[[], sqlite3.Connection],
    cache_loader: Callable[[], Optional[SlackChannelsCache]],
    cache_saver: Callable[[SlackChannelsCache], None],
    slack_client: Optional[SlackChannelsClient],
    expected_owner_id: str,
    clock: Callable[[], datetime],
    event_id_factory: Callable[[], str],
    schedule_id_factory: Callable[[], str],
    owner: UserRef,
    session_id: str,
) -> Callable[[str, str, str], Awaitable[ToolResponse]]:
    """Build the agent-facing ``schedule_create_reminder``
    closure with every DI dependency bound.

    The returned closure exposes ONLY the LLM-visible
    parameters ``(at, recipient_channel, text)``; DI names
    do not appear in the function schema (round-2 reviewer
    L500 fix). Tests in
    :mod:`tests/v2/test_authoring_template_tool` pin the
    signature via ``inspect.signature`` introspection.
    """

    async def schedule_create_reminder(
        at: str,
        recipient_channel: str,
        text: str,
    ) -> ToolResponse:
        # ---- 1. Parse ``at`` ----
        parsed_at, error = _parse_iso_datetime(at)
        if error is not None:
            return error
        assert parsed_at is not None  # mypy hint

        # ---- 2. Resolve channel ----
        cache: Optional[SlackChannelsCache] = cache_loader()
        if cache is None:
            if slack_client is None:
                return ToolResponse.cache_unavailable(
                    kind="slack_channels",
                    network_error="no slack_client configured",
                )
            try:
                cache = refresh_slack_channels(
                    slack_client,
                    expected_owner_id=expected_owner_id,
                    clock=clock,
                )
            except Exception as exc:  # noqa: BLE001
                return ToolResponse.cache_unavailable(
                    kind="slack_channels",
                    network_error=str(exc),
                )
            cache_saver(cache)

        try:
            entry = resolve_channel(
                recipient_channel,
                cache,
                include_archived=False,
            )
        except CacheMiss as exc:
            # Plan §3.3 maps "channel not found after any
            # refresh" to ``not_found`` so the LLM gets a
            # discrete shape it can branch on (vs the
            # cache_unavailable / validation_failed cases).
            return ToolResponse.not_found(
                message=(
                    f"channel {recipient_channel!r} not "
                    f"found in the Slack workspace cache: "
                    f"{exc!s}"
                ),
            )
        except ChannelAmbiguous as exc:
            return ToolResponse.validation_failed(
                issues=[
                    ValidationIssue(
                        code="channel_ambiguous",
                        severity="error",
                        path="recipient_channel",
                        message=(
                            f"{exc.name!r} matches multiple "
                            f"active channels: "
                            f"{exc.candidate_ids!r}"
                        ),
                    )
                ]
            )
        except NoCacheAvailable as exc:
            # Defence in depth — resolver shouldn't reach
            # this branch since we refreshed above.
            return ToolResponse.cache_unavailable(
                kind="slack_channels",
                network_error=(
                    "resolver received cache=None despite "
                    f"refresh attempt: {exc!s}"
                ),
            )

        # Compose the ChannelRef for the builder.
        from app.v2.models.common import ChannelRef

        recipient = ChannelRef(kind="slack", external_id=entry.id)

        # ---- 3. Build spec ----
        schedule_id = schedule_id_factory()
        try:
            spec = build_one_off_reminder(
                at=parsed_at,
                recipient=recipient,
                text=text,
                owner=owner,
                schedule_id=schedule_id,
                clock=clock,
            )
        except ValidationError as exc:
            # Pydantic OneOffReminderArgs constraints
            # (empty / oversize text). Caught BEFORE the
            # ValueError branch because pydantic v2's
            # ValidationError subclasses ValueError.
            return _validation_failed_single(
                code="one_off_reminder_args_invalid",
                path="text",
                message=str(exc),
            )
        except ValueError as exc:
            # build_one_off_reminder raises ValueError on
            # naive `at` / naive `clock`.
            return _validation_failed_single(
                code="build_one_off_reminder_failed",
                path="<root>",
                message=str(exc),
            )

        # ---- 4. Persist draft ----
        draft = ScheduleSpecDraft(
            id=spec.id,
            description=spec.description,
            owner=spec.owner,
            trigger=spec.trigger,
            delivery=spec.delivery,
            failure=spec.failure,
            audit=spec.audit,
            status=spec.status,
            execution_plan_hash=spec.execution_plan_hash,
            template=spec.template,
            parent_hash=spec.parent_hash,
        )
        store.write(session_id, draft)

        # ---- 5. dry_run ----
        dry_run_response = await schedule_dry_run(
            draft.id,
            DryRunMode.VALIDATE_ONLY,
            session_id=session_id,
            store=store,
            handshake_store=handshake_store,
            clock=clock,
        )
        if dry_run_response.status != "ok":
            return dry_run_response

        # ---- 6. Freeze (DB-free; runs BEFORE conn open
        # per round-3 reviewer slice-3 fix — freeze takes no
        # connection and surfaces handshake / trigger /
        # hash-drift gates without touching SQLite. Opening
        # conn_factory() ahead of freeze would mask a freeze
        # failure behind a DB-outage exception if the
        # connection opens fails). ----
        freeze_response = await schedule_freeze(
            draft.id,
            session_id=session_id,
            store=store,
            handshake_store=handshake_store,
            clock=clock,
        )
        if freeze_response.status != "ok":
            return freeze_response

        # ---- 7. Commit (DB write) ----
        with closing(conn_factory()) as conn:
            commit_response = await schedule_draft_commit(
                draft.id,
                session_id=session_id,
                store=store,
                handshake_store=handshake_store,
                conn=conn,
                event_id_factory=event_id_factory,
                clock=clock,
            )

        return commit_response

    schedule_create_reminder.__name__ = (
        SCHEDULE_CREATE_REMINDER_TOOL_NAME
    )
    schedule_create_reminder.__qualname__ = (
        SCHEDULE_CREATE_REMINDER_TOOL_NAME
    )
    return schedule_create_reminder


SCHEDULE_CREATE_RECURRING_SERIES_FROM_SOURCE_TOOL_NAME = (
    "schedule_create_recurring_series_from_source"
)

#: Phase-11 (11A) default per-source cache + live-change
#: policy for a RecurringSeriesFromSource. These are NOT
#: LLM slots (§3.3 slots are source/channel/hour_local/
#: timezone/progress_strategy) — sensible template
#: defaults: ``cache_ttl_seconds=0`` ⇒ probe the loader
#: every fire (the secure default — a recurring series
#: wants fresh content each tick; phase-10 §9c: ttl==0 is
#: the probe-every-fire opt-out), ``USE_LAST_GOOD_SNAPSHOT``
#: so a brief source outage re-serves the last good rather
#: than failing the series, ``LiveChangePolicy.ALLOW`` so a
#: shape change does not block the series (skip_unchanged
#: is the user's drift control, not a hard gate).
_RSFS_CACHE_TTL_SECONDS = 0
_RSFS_STALE_MAX_AGE_SECONDS = 86_400


def make_schedule_create_recurring_series_from_source(
    *,
    store: DraftStore,
    handshake_store: HandshakeStore,
    conn_factory: Callable[[], sqlite3.Connection],
    clock: Callable[[], datetime],
    event_id_factory: Callable[[], str],
    schedule_id_factory: Callable[[], str],
    owner: UserRef,
    session_id: str,
) -> Callable[
    [dict, str, int, str, str], Awaitable[ToolResponse]
]:
    """Build the agent-facing
    ``schedule_create_recurring_series_from_source``
    closure with every DI dependency bound.

    The returned closure exposes ONLY the LLM-visible slot
    set ``(source, channel, hour_local, timezone,
    progress_strategy)``; DI names do NOT appear in the
    function schema (the phase-9
    :func:`make_schedule_create_reminder` DI-leak pin —
    ADK ``FunctionTool`` introspects the closure signature
    to build the model-visible schema). Tests pin the
    signature via ``inspect.signature``.

    Pipeline (closure body), funnelling through the SAME
    7a-extended authoring spine — NO bespoke commit path:

    1. Validate ``source`` (a ``{"loader", "args"}``
       object); unknown loader → ``validation_failed``.
    2. Compose a :class:`SourceRefSpec` with the phase-11
       default cache / live-change policy bundle.
    3. Compile ``(ScheduleSpec, ExecutionPlan)`` via the
       slice-6 :func:`build_recurring_series_from_source`
       builder (inputs + emit, ZERO reasoning). A bad slot
       (hour range / empty channel / empty tz / unknown
       progress_strategy / bad source args) → a typed
       ``validation_failed`` AT the tool boundary.
    4. Persist a :class:`ScheduleSpecDraft` carrying BOTH
       ``execution_plan_hash`` AND the FROZEN
       ``execution_plan`` body (so the 7a commit step-8b
       consistency check ``plan.hash ==
       execution_plan_hash`` passes and the atomic
       ``insert_execution_plan``+``insert_schedule`` path
       is exercised end-to-end).
    5. ``schedule_dry_run(VALIDATE_ONLY)`` → handshake.
    6. ``schedule_freeze`` (cron unlocked at 7a).
    7. ``schedule_draft_commit`` (atomic plan+schedule).
    """

    async def schedule_create_recurring_series_from_source(
        source: dict,
        channel: str,
        hour_local: int,
        timezone: str,
        progress_strategy: str,
    ) -> ToolResponse:
        # Ensure the built-in source loaders are registered
        # in the SOURCES singleton (sanctioned lazy in-fn
        # import — keeps this module's load light + the
        # phase-11 hygiene pin tight).
        import app.v2.sources  # noqa: F401

        # ---- 1. Validate the source slot ----
        if not isinstance(source, dict):
            return _validation_failed_single(
                code="invalid_source",
                path="source",
                message=(
                    "`source` must be an object with a "
                    "'loader' name and optional 'args'"
                ),
            )
        loader = source.get("loader")
        if not isinstance(loader, str) or not loader:
            return _validation_failed_single(
                code="invalid_source",
                path="source.loader",
                message="`source.loader` must be a non-empty string",
            )
        if SOURCES.lookup(loader) is None:
            return _validation_failed_single(
                code="unknown_source_loader",
                path="source.loader",
                message=(
                    f"source loader {loader!r} is not "
                    f"registered in SOURCES"
                ),
            )
        source_args = source.get("args", {})
        if not isinstance(source_args, dict):
            return _validation_failed_single(
                code="invalid_source",
                path="source.args",
                message="`source.args` must be an object",
            )

        # ---- 2. Compose the SourceRefSpec ----
        try:
            source_ref = SourceRefSpec(
                loader=loader,
                args=source_args,
                cache=LiveSourceCachePolicy(
                    cache_ttl_seconds=_RSFS_CACHE_TTL_SECONDS,
                    stale_max_age_seconds=_RSFS_STALE_MAX_AGE_SECONDS,
                    fallback_policy=(
                        SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT
                    ),
                ),
                live_change_policy=LiveChangePolicy.ALLOW,
            )
        except ValidationError as exc:
            return _validation_failed_single(
                code="invalid_source",
                path="source",
                message=str(exc),
            )

        # ---- 3. Build (ScheduleSpec, ExecutionPlan) ----
        schedule_id = schedule_id_factory()
        try:
            spec, plan = build_recurring_series_from_source(
                source=source_ref,
                channel=channel,
                hour_local=hour_local,
                timezone_name=timezone,
                progress_strategy=progress_strategy,
                owner=owner,
                schedule_id=schedule_id,
                clock=clock,
            )
        except ValidationError as exc:
            # Typed bad slot (hour range / empty channel /
            # empty tz / unknown progress_strategy).
            return _validation_failed_single(
                code="recurring_series_args_invalid",
                path="<root>",
                message=str(exc),
            )
        except ValueError as exc:
            # Builder ValueError (naive clock — DI; defensive).
            return _validation_failed_single(
                code="build_recurring_series_failed",
                path="<root>",
                message=str(exc),
            )

        # ---- 4. Persist draft (carries the FROZEN plan
        # body so 7a commit step-8b passes) ----
        draft = ScheduleSpecDraft(
            id=spec.id,
            description=spec.description,
            owner=spec.owner,
            trigger=spec.trigger,
            delivery=spec.delivery,
            failure=spec.failure,
            audit=spec.audit,
            status=spec.status,
            execution_plan_hash=spec.execution_plan_hash,
            template=spec.template,
            parent_hash=spec.parent_hash,
            execution_plan=plan,
        )
        store.write(session_id, draft)

        # ---- 5. dry_run ----
        dry_run_response = await schedule_dry_run(
            draft.id,
            DryRunMode.VALIDATE_ONLY,
            session_id=session_id,
            store=store,
            handshake_store=handshake_store,
            clock=clock,
        )
        if dry_run_response.status != "ok":
            return dry_run_response

        # ---- 6. Freeze (cron unlocked at 7a) ----
        freeze_response = await schedule_freeze(
            draft.id,
            session_id=session_id,
            store=store,
            handshake_store=handshake_store,
            clock=clock,
        )
        if freeze_response.status != "ok":
            return freeze_response

        # ---- 7. Commit (atomic plan + schedule, 7a) ----
        with closing(conn_factory()) as conn:
            commit_response = await schedule_draft_commit(
                draft.id,
                session_id=session_id,
                store=store,
                handshake_store=handshake_store,
                conn=conn,
                event_id_factory=event_id_factory,
                clock=clock,
            )

        return commit_response

    schedule_create_recurring_series_from_source.__name__ = (
        SCHEDULE_CREATE_RECURRING_SERIES_FROM_SOURCE_TOOL_NAME
    )
    schedule_create_recurring_series_from_source.__qualname__ = (
        SCHEDULE_CREATE_RECURRING_SERIES_FROM_SOURCE_TOOL_NAME
    )
    return schedule_create_recurring_series_from_source


__all__ = [
    "SCHEDULE_CREATE_REMINDER_TOOL_NAME",
    "make_schedule_create_reminder",
    "SCHEDULE_CREATE_RECURRING_SERIES_FROM_SOURCE_TOOL_NAME",
    "make_schedule_create_recurring_series_from_source",
]
