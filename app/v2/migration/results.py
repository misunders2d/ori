"""V2 scheduler — migration typed result models.

Phase 16 slice 1 per ``docs/PHASE_16_PLAN.md`` §3 + the
claude-reviewer round-1 disposition (Q3 honest-mapping cut /
Q4 / §3.5 typed-result discipline carried 15→16). Dedicated
typed Pydantic models — never loose dicts.

A v1 ``Contract`` is migrated to a v2 ``ScheduleSpec`` ONLY
when it is structurally v2-expressible (Q3). Anything that is
NOT faithfully representable is SKIPPED with an explicit
per-contract reason — NEVER silently coerced / lossily
partial-migrated (the phase-11-ChannelDigest-(b) + §13
audit-truth discipline).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §12 step 16
- ``docs/PHASE_16_PLAN.md`` §3 / §9
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.v2.enums import DeliveryFallbackPolicy


class MigrationStatus(str, Enum):
    """A v1 contract's migration assessment outcome.

    ``migratable_with_binding`` — structurally v2-expressible;
        a v2 ``ScheduleSpec`` CAN be built, but v2 requires two
        fields v1 does not carry 1:1 (``owner.platform`` — v1
        ``author`` is a bare id with no platform; and
        ``delivery.target_session_id`` — the v1 emit
        destination is an adapter-specific template arg, NOT a
        schema field). These are operator-supplied bindings,
        explicitly provided, NEVER invented.
    ``skipped`` — NOT faithfully representable in a v2
        ``ScheduleSpec`` (non-cron trigger / reasoning-bearing
        / deterministic inputs / non-plain emit / sub-minimum
        description / non-default failure or acceptance
        semantics). Honestly flagged with the reason(s); never
        coerced or partial-migrated.
    """

    MIGRATABLE_WITH_BINDING = "migratable_with_binding"
    SKIPPED = "skipped"


class ContractMigrationAssessment(BaseModel):
    """The pure structural verdict for one v1 contract."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    version: int
    status: MigrationStatus
    skip_reasons: list[str] = Field(
        default_factory=list,
        description="Non-empty IFF status == skipped; each is "
        "a precise, honest reason (no silent drop).",
    )
    required_bindings: list[str] = Field(
        default_factory=list,
        description="Operator-supplied binding keys needed to "
        "build the v2 spec (status == migratable_with_binding) "
        "— e.g. ['owner.platform', "
        "'delivery.target_session_id']. NEVER auto-invented.",
    )


class MigrationPlanReport(BaseModel):
    """The dry-run enumeration over every v1 contract — the
    honest MIGRATABLE-vs-SKIPPED report (Q7). Pure: producing
    it performs ZERO v2 write and ZERO v1 mutation."""

    model_config = ConfigDict(extra="forbid")

    entries: list[ContractMigrationAssessment] = Field(
        default_factory=list
    )
    migratable_count: int = 0
    skipped_count: int = 0


class MigrationBinding(BaseModel):
    """Operator-supplied, per-contract bindings for the two v2
    fields v1 does not carry 1:1. Explicit — the mapper NEVER
    fabricates a platform or a delivery target."""

    model_config = ConfigDict(extra="forbid")

    platform: str = Field(
        description="v2 UserRef.platform (slack / telegram / "
        "email). v1 `author` carries no platform — operator "
        "supplies it."
    )
    user_id: Optional[str] = Field(
        default=None,
        description="v2 UserRef.user_id; defaults to the v1 "
        "contract `author` when not overridden.",
    )
    target_session_id: str = Field(
        description="v2 Delivery.target_session_id. The v1 "
        "emit destination is an adapter-specific template arg "
        "(NOT a schema field) — operator supplies the v2 "
        "target explicitly."
    )
    fallback_policy: DeliveryFallbackPolicy = (
        DeliveryFallbackPolicy.SESSION_TO_ORIGIN
    )


class MigrationOutcome(str, Enum):
    """Per-contract outcome of a :func:`migrate_contracts` pass.

    ``dry_run_would_migrate`` — confirm=False (the DEFAULT):
        the contract is migratable + bound; NOTHING was
        written (dry-run is PURE).
    ``migrated`` — confirm=True: the v2 schedule +
        ``schedule_created`` + ``migration_v1_to_v2_complete``
        lineage event were written ATOMICALLY (both-or-neither).
    ``skipped_not_migratable`` — the structural Q3 cut
        rejected it (honest skip_reasons).
    ``skipped_no_binding`` — migratable but the operator
        supplied no :class:`MigrationBinding` for it (v2
        owner.platform / delivery.target_session_id are
        operator-supplied, NEVER fabricated).
    ``skipped_already_exists`` — a v2 schedule with this id
        already exists (idempotent re-run; NO duplicate
        schedule, NO duplicate lineage event).
    """

    DRY_RUN_WOULD_MIGRATE = "dry_run_would_migrate"
    MIGRATED = "migrated"
    SKIPPED_NOT_MIGRATABLE = "skipped_not_migratable"
    SKIPPED_NO_BINDING = "skipped_no_binding"
    SKIPPED_ALREADY_EXISTS = "skipped_already_exists"


class MigrationCommitEntry(BaseModel):
    """One contract's :func:`migrate_contracts` result."""

    model_config = ConfigDict(extra="forbid")

    contract_id: str
    outcome: MigrationOutcome
    schedule_id: Optional[str] = None
    skip_reasons: list[str] = Field(default_factory=list)
    lineage_event_id: Optional[str] = Field(
        default=None,
        description="The migration_v1_to_v2_complete event id "
        "(set only on outcome == migrated).",
    )


class MigrationCommitReport(BaseModel):
    """The full :func:`migrate_contracts` pass report. When
    ``dry_run`` is True NOTHING was written (PURE)."""

    model_config = ConfigDict(extra="forbid")

    dry_run: bool
    entries: list[MigrationCommitEntry] = Field(default_factory=list)
    migrated_count: int = 0
    skipped_count: int = 0


class ContractNotMigratable(Exception):
    """Raised by ``contract_to_schedule_spec`` when the
    contract is ``SKIPPED`` (not structurally v2-expressible).
    The caller must consult the assessment first — a
    SKIPPED contract is NEVER partial/lossy-migrated."""


__all__ = [
    "MigrationStatus",
    "ContractMigrationAssessment",
    "MigrationPlanReport",
    "MigrationBinding",
    "MigrationOutcome",
    "MigrationCommitEntry",
    "MigrationCommitReport",
    "ContractNotMigratable",
]
