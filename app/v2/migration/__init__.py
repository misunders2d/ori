"""V2 scheduler — migration tooling package (§12 step 16).

Phase 16. Build-the-layer v1→v2 migration: pure
``Contract``→``ScheduleSpec`` mapping + the honest
MIGRATABLE-vs-SKIPPED dry-run. v1 READ-ONLY (reads ONLY the
shipped ``ContractStore`` pure-read API; NEVER any v1 write
path). Slice 1 = pure mapping + dry-run (ZERO write). The
GATED idempotent migrate + ledger backfill is slice 2; it is
NOT auto-invoked at boot, NOT binding-wired.

§12 step 16 is the LAST core step (RELAY-TERMINUS — see
``docs/PHASE_16_PLAN.md`` §8).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §12 step 16
- ``docs/PHASE_16_PLAN.md``
"""

from __future__ import annotations

from app.v2.migration.commit import migrate_contracts
from app.v2.migration.mapper import (
    assess_contract,
    contract_to_schedule_spec,
    migration_dry_run,
)
from app.v2.migration.results import (
    ContractMigrationAssessment,
    ContractNotMigratable,
    MigrationBinding,
    MigrationCommitEntry,
    MigrationCommitReport,
    MigrationOutcome,
    MigrationPlanReport,
    MigrationStatus,
)

__all__ = [
    "assess_contract",
    "contract_to_schedule_spec",
    "migration_dry_run",
    "migrate_contracts",
    "MigrationStatus",
    "ContractMigrationAssessment",
    "MigrationPlanReport",
    "MigrationBinding",
    "MigrationOutcome",
    "MigrationCommitEntry",
    "MigrationCommitReport",
    "ContractNotMigratable",
]
