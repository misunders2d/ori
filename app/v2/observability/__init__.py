"""V2 scheduler — observability package (§12 step 15).

Phase 15 slice 1. Pure read-only query / aggregation
primitives over the live EventLedger substrate (written every
fire by the phase-9–14 cores) — a pure side-channel: ZERO
fire-path control flow, no event the dedup / terminal /
failure cores do not already write, no new EventKind, no
v002. Composes ONLY the shipped ``storage`` /
``registry_cache`` pure-read surface (no SQL
re-implementation).

The five shipped primitives (``schedule_diff`` +
``schedule_replay`` are DEFERRED — see
``docs/PHASE_15_PLAN.md`` §9 + the closeout §9
reconciliation). The background failure-monitor pure detector
is slice 2 (build-the-layer; NOT wired).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §9
- ``docs/PHASE_15_PLAN.md``
"""

from __future__ import annotations

from app.v2.observability.primitives import (
    registry_status,
    schedule_failures,
    schedule_health,
    schedule_history,
    schedule_status,
)
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

__all__ = [
    # primitives
    "schedule_status",
    "schedule_failures",
    "schedule_history",
    "schedule_health",
    "registry_status",
    # result models
    "RunSummary",
    "ScheduleStatusResult",
    "FailureEvent",
    "ScheduleFailuresResult",
    "HistoryEntry",
    "ScheduleHistoryResult",
    "ScheduleHealthResult",
    "RegistryCacheStatus",
    "RegistryStatusResult",
]
