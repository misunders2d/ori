"""V2 scheduler — observability package (§12 step 15).

Phase 15 slice 1. Pure read-only query / aggregation
primitives over the live EventLedger substrate (written every
fire by the phase-9–14 cores) — a pure side-channel: ZERO
fire-path control flow, no event the dedup / terminal /
failure cores do not already write, no new EventKind, no
v002. Composes ONLY the shipped ``storage`` /
``registry_cache`` pure-read surface (no SQL
re-implementation).

Exactly FIVE primitives are shipped: ``schedule_status``,
``schedule_failures``, ``schedule_history``,
``schedule_health``, ``registry_status``. ``schedule_diff``
and ``schedule_replay`` are DEFERRED — NOT shipped, NOT §15
pure-read primitives (see ``docs/PHASE_15_PLAN.md`` §9.1 +
the closeout §9 reconciliation: no persisted historical
ScheduleSpec body to diff; replay touches the dry-run / fire
path). The background failure-monitor pure detector is slice
2 (build-the-layer; NOT wired — no periodic loop, no
re-alert dispatch).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §9
- ``docs/PHASE_15_PLAN.md``
"""

from __future__ import annotations

from app.v2.observability.failure_monitor import failure_monitor_scan
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
    OverdueAlert,
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
    # failure-monitor pure detector (build-the-layer, NOT wired)
    "failure_monitor_scan",
    "OverdueAlert",
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
