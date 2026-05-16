"""V2 scheduler — observability pure-read primitives.

Phase 15 slice 1 per ``docs/PHASE_15_PLAN.md`` §1/§3/§4 + the
claude-reviewer round-1 disposition (Q1–Q7) + the slice-1
``schedule_diff`` fork ruling (α).

The five SHIPPED primitives (Q3 IN-set amended 6→5;
``schedule_diff`` DEFERRED — see §9 of the plan + the closeout
§9 reconciliation: there is NO persisted historical
ScheduleSpec body to diff — ``schedules`` is one-row-per-id
current-only, ``schedule_created`` payload is
``{hash, template}`` only, no ``schedule_revised`` emitter
exists, ``execution_plans`` stores ExecutionPlan not
ScheduleSpec bodies; a real §9 ``schedule_diff`` needs a
spec-version store NOT shipped — its own future design+DDL
decision, NOT a §15 pure-read primitive. ``schedule_replay``
likewise deferred — it touches the dry-run/fire path):

- :func:`schedule_status`
- :func:`schedule_failures`  (per-schedule)
- :func:`schedule_history`
- :func:`schedule_health`    (DI-clock)
- :func:`registry_status`    (reuses phase-6 registry_cache)

Every primitive is a PURE READ: it composes ONLY the shipped
``storage/events.py`` / ``storage/schedules.py`` /
``registry_cache`` read surface — NO SQL re-implementation, NO
mutation, NO new EventKind, NO fire-path touch. The
EventLedger is the source of truth (§9 "primitives over
EventLedger"); it is written every fire by the phase-9–14
cores — observability is a pure read-only side-channel over
that already-live substrate.

``now`` is always an injected caller clock (DI) — this module
imports no clock, keeping ``_defaults.py`` the sole
``datetime.now`` site (the phase-5 hard rule 10 carried
through 11–14).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §9
- ``docs/PHASE_15_PLAN.md`` §1 / §3 / §4 / §9
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from app.v2.enums import EventKind
from app.v2.models.event import Event
from app.v2.observability.results import (
    FailureEvent,
    HistoryEntry,
    RegistryCacheStatus,
    RegistryStatusResult,
    RunSummary,
    ScheduleFailuresResult,
    ScheduleHealthResult,
    ScheduleHistoryResult,
    ScheduleStatusResult,
)
from app.v2.registry_cache import is_stale, load_cache
from app.v2.storage.events import list_events_for_schedule
from app.v2.storage.schedules import get_schedule

# Default-windowed scan ceilings. The EventLedger read is
# bounded (no LIMIT -1); a generous default keeps the typical
# diagnostic call complete while staying explicitly capped.
_DEFAULT_EVENT_SCAN = 1000
_DEFAULT_RECENT = 200
_DEFAULT_FAILURE_LIMIT = 50

_FAILURE_KINDS: tuple[EventKind, ...] = (
    EventKind.RUN_FAILED,
    EventKind.EMIT_FAILED,
    EventKind.SOURCE_FAILED,
    EventKind.DELIVERY_FAILED,
)

# Registry-cache kinds — the phase-6 on-disk cache set.
_REGISTRY_KINDS: tuple[str, ...] = (
    "slack_channels",
    "google_sheets_items",
    "google_docs_items",
)


def _run_summaries(events: list[Event]) -> list[RunSummary]:
    """Fold the schedule's events into per-run summaries.

    Pure in-memory aggregation over the ledger rows — no
    ``runs``-table read (the ledger is the source of truth,
    §9). Ordered by first-seen ``run_created`` ts ascending so
    the output is deterministic.
    """
    by_run: dict[str, dict] = {}
    order: list[str] = []
    for ev in events:
        rid = ev.run_id
        if rid is None:
            continue
        slot = by_run.get(rid)
        if slot is None:
            slot = {
                "created_at": None,
                "started_at": None,
                "completed_at": None,
                "status": "unknown",
                "rank": -1,
            }
            by_run[rid] = slot
            order.append(rid)
        k = ev.kind
        if k is EventKind.RUN_CREATED:
            slot["created_at"] = ev.ts
            slot["status"] = _max_status(slot["status"], "created", slot)
        elif k is EventKind.RUN_CLAIMED:
            slot["status"] = _max_status(slot["status"], "claimed", slot)
        elif k in (
            EventKind.RUN_STARTED,
            EventKind.RUN_RECOVERED,
        ):
            slot["started_at"] = slot["started_at"] or ev.ts
            slot["status"] = _max_status(slot["status"], "running", slot)
        elif k is EventKind.RUN_SUCCEEDED:
            slot["completed_at"] = ev.ts
            slot["status"] = "succeeded"
            slot["rank"] = 99
        elif k is EventKind.RUN_FAILED:
            slot["completed_at"] = ev.ts
            slot["status"] = "failed"
            slot["rank"] = 99
        elif k is EventKind.RUN_CANCELLED:
            slot["completed_at"] = ev.ts
            slot["status"] = "cancelled"
            slot["rank"] = 99

    out: list[RunSummary] = []
    for rid in order:
        s = by_run[rid]
        dur: Optional[int] = None
        if s["started_at"] is not None and s["completed_at"] is not None:
            dur = int(
                (s["completed_at"] - s["started_at"]).total_seconds()
                * 1000
            )
        out.append(
            RunSummary(
                run_id=rid,
                status=s["status"],
                created_at=s["created_at"],
                started_at=s["started_at"],
                completed_at=s["completed_at"],
                duration_ms=dur,
            )
        )
    return out


_NONTERMINAL_RANK = {"unknown": 0, "created": 1, "claimed": 2, "running": 3}


def _max_status(current: str, candidate: str, slot: dict) -> str:
    """Keep the furthest-reached NON-terminal status; a
    terminal status (rank 99) is sticky and set directly by
    the caller, never downgraded here."""
    if slot["rank"] >= 99:
        return current
    if _NONTERMINAL_RANK.get(candidate, 0) >= _NONTERMINAL_RANK.get(
        current, 0
    ):
        return candidate
    return current


def schedule_status(
    conn: sqlite3.Connection,
    schedule_id: str,
    *,
    recent_limit: int = _DEFAULT_RECENT,
) -> ScheduleStatusResult:
    """Recent runs + pause/archive state + unacked-alert
    count, derived purely from ``get_schedule`` +
    ``list_events_for_schedule``."""
    spec = get_schedule(conn, schedule_id)
    if spec is None:
        return ScheduleStatusResult(
            schedule_id=schedule_id, found=False
        )

    events = list_events_for_schedule(
        conn, schedule_id, limit=recent_limit
    )
    sent_ids = {
        e.id for e in events if e.kind is EventKind.ADMIN_ALERT_SENT
    }
    acked = {
        e.correlates
        for e in events
        if e.kind is EventKind.ADMIN_ALERT_ACKED
        and e.correlates is not None
    }
    return ScheduleStatusResult(
        schedule_id=schedule_id,
        found=True,
        status=spec.status.value,
        description=spec.description,
        recent_runs=_run_summaries(events),
        unacked_alert_count=len(sent_ids - acked),
    )


def schedule_failures(
    conn: sqlite3.Connection,
    schedule_id: str,
    *,
    limit: int = _DEFAULT_FAILURE_LIMIT,
) -> ScheduleFailuresResult:
    """The most-recent ``limit`` failure-kind events for the
    schedule (run/emit/source/delivery _failed), merged across
    kinds, most-recent-first.

    Per-schedule by construction: the shipped EventLedger read
    surface is per-schedule; slice-1 may NOT add a global
    cross-schedule read (Q3 condition — a decomposition of
    schedule_status-over-EventLedger, no new design surface).
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1; got {limit}")
    merged: list[Event] = []
    for k in _FAILURE_KINDS:
        merged.extend(
            list_events_for_schedule(
                conn, schedule_id, kind=k, limit=limit
            )
        )
    merged.sort(key=lambda e: e.ts, reverse=True)
    return ScheduleFailuresResult(
        schedule_id=schedule_id,
        limit=limit,
        failures=[
            FailureEvent(
                event_id=e.id,
                run_id=e.run_id,
                kind=e.kind.value,
                ts=e.ts,
                payload=e.payload,
            )
            for e in merged[:limit]
        ],
    )


def schedule_history(
    conn: sqlite3.Connection,
    schedule_id: str,
    *,
    limit: int = _DEFAULT_EVENT_SCAN,
) -> ScheduleHistoryResult:
    """Chronological timeline (versions + Runs + key events).

    ``list_events_for_schedule`` returns most-recent-first;
    reversed here so the timeline reads oldest→newest."""
    events = list_events_for_schedule(
        conn, schedule_id, limit=limit
    )
    chrono = list(reversed(events))
    return ScheduleHistoryResult(
        schedule_id=schedule_id,
        total=len(chrono),
        timeline=[
            HistoryEntry(
                event_id=e.id,
                run_id=e.run_id,
                kind=e.kind.value,
                ts=e.ts,
            )
            for e in chrono
        ],
    )


def schedule_health(
    conn: sqlite3.Connection,
    schedule_id: str,
    *,
    now: datetime,
    window: timedelta = timedelta(days=7),
    scan_limit: int = _DEFAULT_EVENT_SCAN,
) -> ScheduleHealthResult:
    """Fire-OK rate over ``window`` ending at ``now`` (DI
    clock — this module imports no clock).

    ``fire_ok_rate`` is ``None`` (NOT 0.0) when there is no
    terminal run in the window — an explicit no-data signal,
    never a silent div-by-zero."""
    cutoff = now - window
    events = list_events_for_schedule(
        conn, schedule_id, limit=scan_limit
    )
    succeeded = sum(
        1
        for e in events
        if e.kind is EventKind.RUN_SUCCEEDED and e.ts >= cutoff
    )
    failed = sum(
        1
        for e in events
        if e.kind is EventKind.RUN_FAILED and e.ts >= cutoff
    )
    total = succeeded + failed
    rate = (succeeded / total) if total > 0 else None
    return ScheduleHealthResult(
        schedule_id=schedule_id,
        window_seconds=int(window.total_seconds()),
        succeeded=succeeded,
        failed=failed,
        fire_ok_rate=rate,
    )


def registry_status(
    *,
    now: datetime,
    base=None,
) -> RegistryStatusResult:
    """Per registry-cache kind: presence + ``fetched_at`` +
    staleness, REUSING the shipped phase-6 ``load_cache`` /
    ``is_stale`` — NO re-implementation, NO DB (the registry
    cache is on-disk, not the v2 SQLite store)."""
    caches: list[RegistryCacheStatus] = []
    for kind in _REGISTRY_KINDS:
        cache = load_cache(kind, base=base)  # type: ignore[arg-type]
        if cache is None:
            caches.append(
                RegistryCacheStatus(kind=kind, present=False)
            )
            continue
        caches.append(
            RegistryCacheStatus(
                kind=kind,
                present=True,
                fetched_at=cache.fetched_at,
                stale=is_stale(cache, now=now),
            )
        )
    return RegistryStatusResult(caches=caches)


__all__ = [
    "schedule_status",
    "schedule_failures",
    "schedule_history",
    "schedule_health",
    "registry_status",
]
