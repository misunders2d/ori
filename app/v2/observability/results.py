"""V2 scheduler — observability typed result models.

Phase 15 slice 1 per ``docs/PHASE_15_PLAN.md`` §3 + the
claude-reviewer round-1 disposition (Q4: ToolResponse was
code-verified insufficient — its ``ok`` payload allowlist is
``{draft_id, schedule_id, spec, message}`` with ``spec`` a
loose ``dict[str, Any]`` and an ``extra="forbid"`` +
status-allowlist validator; it carries no arbitrary typed
observability payload). So every observability primitive
returns its OWN dedicated typed Pydantic model here — never a
loose dict, and ``ToolResponse`` is NOT mutated (byte-safe;
avoids the phase-7–14 authoring-contract regression surface).

All models are pure data shapes. ``payload`` fields carry the
verbatim EventLedger ``Event.payload`` (itself
``dict[str, Any]`` in the shipped ``app.v2.models.event``
contract) — that is the ledger's own shape, not a
freshly-invented loose API dict.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §9 (observability primitives)
- ``docs/PHASE_15_PLAN.md`` §3 / §9
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class RunSummary(BaseModel):
    """One Run's lifecycle, derived purely from its
    EventLedger rows (no ``runs``-table read needed — the
    ledger is the source of truth, §9)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str = Field(
        description=(
            "Last-known run status derived from the latest "
            "lifecycle event: a terminal kind "
            "(succeeded/failed/cancelled) when present, else "
            "the furthest-reached non-terminal "
            "(running/claimed/created)."
        ),
    )
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = Field(
        default=None,
        description=(
            "completed_at - started_at in integer ms; None "
            "unless BOTH are present."
        ),
    )


class ScheduleStatusResult(BaseModel):
    """`schedule_status(id)` — recent runs + pause/archive
    state + unacked-alert count, all over the EventLedger."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: str
    found: bool
    status: Optional[str] = Field(
        default=None,
        description="ScheduleStatus value; None when not found.",
    )
    description: Optional[str] = None
    recent_runs: list[RunSummary] = Field(default_factory=list)
    unacked_alert_count: int = 0


class FailureEvent(BaseModel):
    """A single failure-kind ledger event."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    run_id: Optional[str] = None
    kind: str
    ts: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class ScheduleFailuresResult(BaseModel):
    """`schedule_failures(id, limit)` — recent failure events
    (run_failed | emit_failed | source_failed |
    delivery_failed), most-recent-first.

    Per-schedule (the shipped EventLedger read surface is
    per-schedule; no global cross-schedule read exists and
    slice-1 may not add one — Q3 condition: a decomposition
    of schedule_status-over-EventLedger, no new design
    surface)."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: str
    limit: int
    failures: list[FailureEvent] = Field(default_factory=list)


class HistoryEntry(BaseModel):
    """One timeline entry (chronological)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    run_id: Optional[str] = None
    kind: str
    ts: datetime


class ScheduleHistoryResult(BaseModel):
    """`schedule_history(id)` — chronological timeline of
    versions + Runs + key events."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: str
    total: int
    timeline: list[HistoryEntry] = Field(default_factory=list)


class ScheduleHealthResult(BaseModel):
    """`schedule_health(id)` — fire-OK rate over a window."""

    model_config = ConfigDict(extra="forbid")

    schedule_id: str
    window_seconds: int
    succeeded: int
    failed: int
    fire_ok_rate: Optional[float] = Field(
        default=None,
        description=(
            "succeeded / (succeeded + failed) within the "
            "window; None when there is NO terminal run in "
            "the window (explicit — never a silent 0.0 / "
            "div-by-zero)."
        ),
    )


class RegistryCacheStatus(BaseModel):
    """One registry-cache kind's presence + staleness."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    present: bool
    fetched_at: Optional[datetime] = None
    stale: Optional[bool] = Field(
        default=None,
        description="None when the cache file is absent.",
    )


class OverdueAlert(BaseModel):
    """One unacked-AND-overdue ``admin_alert_sent`` ledger
    row, returned by the build-the-layer failure-monitor
    pure detector (``failure_monitor_scan``). The detector is
    NOT wired — no periodic loop, no re-alert dispatch (Q1/Q2
    build-the-layer; deferred, closeout-recorded)."""

    model_config = ConfigDict(extra="forbid")

    alert_event_id: str = Field(
        description="The admin_alert_sent event id (the id "
        "that an admin_alert_acked.correlates would clear).",
    )
    schedule_id: str
    run_id: Optional[str] = None
    sent_ts: datetime
    age_seconds: int = Field(
        description="now - sent_ts in whole seconds (>= the "
        "threshold by construction).",
    )


class RegistryStatusResult(BaseModel):
    """`registry_status` — cached enums + staleness, REUSING
    the shipped phase-6 ``registry_cache`` read surface
    (``load_cache`` / ``is_stale``); NO re-implementation."""

    model_config = ConfigDict(extra="forbid")

    caches: list[RegistryCacheStatus] = Field(default_factory=list)


__all__ = [
    "RunSummary",
    "ScheduleStatusResult",
    "FailureEvent",
    "ScheduleFailuresResult",
    "HistoryEntry",
    "ScheduleHistoryResult",
    "ScheduleHealthResult",
    "RegistryCacheStatus",
    "RegistryStatusResult",
    "OverdueAlert",
]
