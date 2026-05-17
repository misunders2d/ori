"""V2 scheduler — pure v1→v2 migration mapper + dry-run.

Phase 16 slice 1 per ``docs/PHASE_16_PLAN.md`` §1/§3/§4 + the
claude-reviewer round-1 disposition (Q1–Q7) + the GO-slice-1
binding criteria. Build-the-layer; PURE; NO write; NO CLI; NO
backfill (slice 2).

**v1 READ-ONLY invariant (BINDING, PHASE_16_PLAN §0.2).** This
module reads v1 ONLY through the shipped pure-read
``app.contracts.store.ContractStore`` API
(``list_all`` / ``load_latest``) + the ``app.contracts.schema``
models. It NEVER imports ``app.contracts.executor`` /
``app.tasks`` / ``app.scheduler_instance`` / any v1 write
path. ZERO v1 mutation, ZERO v1 behaviour change — v1 keeps
running its contracts live, unchanged.

**Honest mapping cut (Q3).** A v1 ``Contract`` migrates to a
v2 ``ScheduleSpec`` ONLY when it is structurally
v2-expressible. Everything else is SKIPPED with an explicit
per-contract reason — NEVER silently coerced or
lossily-partial-migrated (the phase-11-ChannelDigest-(b) +
§13 audit-truth discipline; a migrated v2 spec must not
misrepresent the v1 contract). The two v2 fields v1 does not
carry 1:1 (``owner.platform`` / ``delivery.target_session_id``)
are operator-supplied :class:`MigrationBinding` values —
explicitly provided, NEVER invented.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §12 step 16
- ``docs/PHASE_16_PLAN.md`` §1 / §3 / §9
"""

from __future__ import annotations

# v1 READ-ONLY surface — pure-read store + schema models ONLY.
# (NO app.contracts.executor / app.tasks / app.scheduler_instance.)
from app.contracts.schema import (
    Acceptance,
    Contract,
    CronTrigger as V1CronTrigger,
    FailureAction,
)
from app.contracts.store import ContractStore
from app.v2.migration.results import (
    ContractMigrationAssessment,
    ContractNotMigratable,
    MigrationBinding,
    MigrationPlanReport,
    MigrationStatus,
)
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import CronTrigger as V2CronTrigger

_V2_MIN_DESCRIPTION = 8
_REQUIRED_BINDINGS = ["owner.platform", "delivery.target_session_id"]


def assess_contract(contract: Contract) -> ContractMigrationAssessment:
    """Pure structural verdict for one v1 contract.

    Collects ALL honest skip reasons (not first-fail — the
    ``validate_schedule_spec`` all-issues discipline) so the
    dry-run report tells the operator every reason a contract
    is not v2-expressible.
    """
    reasons: list[str] = []

    if not isinstance(contract.trigger, V1CronTrigger):
        reasons.append(
            f"trigger type {type(contract.trigger).__name__!r} has "
            "no faithful v2 analogue — only cron migrates "
            "(OnDemand / Event deferred)"
        )
    if contract.reasoning:
        reasons.append(
            "reasoning-bearing contract — needs a v2 "
            "ExecutionPlan port (deferred §12 step 16)"
        )
    if contract.inputs:
        reasons.append(
            "deterministic InputSpec fetch has no v2 "
            "ScheduleSpec representation (deferred)"
        )
    if len(contract.emit) != 1:
        reasons.append(
            f"emit count {len(contract.emit)} != 1 — "
            "multi-emit not v2-ScheduleSpec-expressible "
            "(deferred)"
        )
    else:
        only_emit = contract.emit[0]
        if only_emit.gate is not None or only_emit.abort_on_gate_fail:
            reasons.append(
                "emit gate / abort_on_gate_fail not "
                "v2-ScheduleSpec-expressible (deferred)"
            )
    if len(contract.description.strip()) < _V2_MIN_DESCRIPTION:
        reasons.append(
            f"description shorter than the v2 minimum "
            f"({_V2_MIN_DESCRIPTION} chars)"
        )
    if contract.on_failure != FailureAction():
        reasons.append(
            "non-default v1 on_failure — not faithfully "
            "representable as a v2 FailurePolicy default "
            "(deferred; not silently flattened)"
        )
    if contract.acceptance != Acceptance():
        reasons.append(
            "non-default v1 acceptance (global checks) has no "
            "v2 ScheduleSpec representation (deferred)"
        )

    if reasons:
        return ContractMigrationAssessment(
            contract_id=contract.id,
            version=contract.version,
            status=MigrationStatus.SKIPPED,
            skip_reasons=reasons,
        )
    return ContractMigrationAssessment(
        contract_id=contract.id,
        version=contract.version,
        status=MigrationStatus.MIGRATABLE_WITH_BINDING,
        required_bindings=list(_REQUIRED_BINDINGS),
    )


def contract_to_schedule_spec(
    contract: Contract,
    *,
    binding: MigrationBinding,
) -> ScheduleSpec:
    """Map a structurally-v2-expressible v1 contract to an
    in-memory v2 ``ScheduleSpec`` (UNFROZEN — ``hash=""``; this
    is the pure slice-1 artifact, ZERO write / no
    ``with_fresh_hash``).

    Raises :class:`ContractNotMigratable` (carrying the honest
    skip reasons) when the contract is SKIPPED — a SKIPPED
    contract is NEVER partial / lossy-migrated.
    """
    assessment = assess_contract(contract)
    if assessment.status is MigrationStatus.SKIPPED:
        raise ContractNotMigratable(
            f"contract {contract.id!r} is not v2-expressible: "
            f"{assessment.skip_reasons}"
        )

    # Structurally guaranteed a v1 CronTrigger by assess_contract.
    v1_trigger = contract.trigger
    assert isinstance(v1_trigger, V1CronTrigger)

    return ScheduleSpec(
        id=contract.id,
        owner=UserRef(
            platform=binding.platform,
            user_id=binding.user_id or contract.author,
        ),
        description=contract.description,
        trigger=V2CronTrigger(
            cron=v1_trigger.cron,
            timezone=v1_trigger.timezone,
        ),
        delivery=Delivery(
            target_session_id=binding.target_session_id,
            fallback_policy=binding.fallback_policy,
        ),
        failure=FailurePolicy(),
        audit=AuditPolicy(),
    )


def migration_dry_run(store: ContractStore) -> MigrationPlanReport:
    """Iterate every v1 contract (latest version) via the
    shipped pure-read ``ContractStore`` and return the honest
    MIGRATABLE-vs-SKIPPED enumeration.

    PURE: ZERO v2 write, ZERO v1 mutation — it only calls the
    read methods ``list_all`` / ``load_latest``.
    """
    entries: list[ContractMigrationAssessment] = []
    for contract_id in store.list_all():
        contract = store.load_latest(contract_id)
        entries.append(assess_contract(contract))

    migratable = sum(
        1
        for e in entries
        if e.status is MigrationStatus.MIGRATABLE_WITH_BINDING
    )
    return MigrationPlanReport(
        entries=entries,
        migratable_count=migratable,
        skipped_count=len(entries) - migratable,
    )


__all__ = [
    "assess_contract",
    "contract_to_schedule_spec",
    "migration_dry_run",
]
