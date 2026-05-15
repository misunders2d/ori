"""V2 scheduler runtime layer.

Phase 4 ships the runtime over the phase-3 storage primitives:
state machine, claim, recovery scan, worker loop, wakeup
callback. Each lives in its own module + re-exports here as
slices land.

The worker body is intentionally empty in phase 4 — runs go
``pending → claimed → running → succeeded`` without doing any
external work. Real reasoning + emit lands in later phases.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.1, §4.0.4, §6.1
- ``docs/PHASE_4_PLAN.md``
"""

from app.v2.runtime.claim import claim_run
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


__all__ = [
    "IllegalTransitionError",
    "LEGAL_TRANSITIONS",
    "RecoveredRun",
    "RecoveryError",
    "assert_legal_transition",
    "claim_run",
    "is_legal_transition",
    "scan_stale_runs",
]
