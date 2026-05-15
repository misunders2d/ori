"""V2 scheduler runtime layer.

Phase 4 ships the runtime over the phase-3 storage primitives:
state machine, claim, recovery scan, worker loop, wakeup
callback. Phase 5 adds production-wiring foundations: shared
cron guards and the production clock + id factories.

The worker body is intentionally empty in phase 4 — runs go
``pending → claimed → running → succeeded`` without doing any
external work. Real reasoning + emit lands in later phases.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.1, §4.0.4, §6.1
- ``docs/PHASE_4_PLAN.md``
- ``docs/PHASE_5_PLAN.md``
"""

from app.v2.runtime._defaults import (
    prod_clock,
    prod_event_id_factory,
    prod_run_id_factory,
)
from app.v2.runtime.claim import claim_run
from app.v2.runtime.cron_guard import reject_numeric_dow
from app.v2.runtime.recovery import (
    RecoveredRun,
    RecoveryError,
    scan_stale_runs,
)
from app.v2.runtime.state_machine import (
    IllegalTransitionError,
    LEGAL_TRANSITIONS,
    assert_legal_transition,
    is_legal_transition,
)
from app.v2.runtime.wakeup import wakeup
from app.v2.runtime.worker import Worker


__all__ = [
    "IllegalTransitionError",
    "LEGAL_TRANSITIONS",
    "RecoveredRun",
    "RecoveryError",
    "Worker",
    "assert_legal_transition",
    "claim_run",
    "is_legal_transition",
    "prod_clock",
    "prod_event_id_factory",
    "prod_run_id_factory",
    "reject_numeric_dow",
    "scan_stale_runs",
    "wakeup",
]
