"""V2 scheduler — GATED idempotent v1→v2 migrate + lineage backfill.

Phase 16 slice 2 per ``docs/PHASE_16_PLAN.md`` §1/§4/§9 + the
claude-reviewer round-1 disposition (Q1–Q7) + the slice-2
backfill fork ruling (α). The gated-WRITE slice.

**Backfill = (α) lineage, NOT per-fire replay.** There is NO
shipped pure-read v1-fire-history API (``ContractStore`` is
contracts-only; ``data/contract_audit/*.jsonl`` /
``contract_failures.jsonl`` have no shipped reader). Per the
slice-2 fork ruling, "ledger backfill" is the §13 audit-truth
INTENT realised honestly: exactly ONE shipped
``MIGRATION_V1_TO_V2_COMPLETE`` event (already in the v001
EventKind CHECK — no new EventKind, no v002) per migrated
schedule, payload provenance-marked
(``migrated_from: <contract_id>@<hash>`` + ``backfill: True``)
so the ledger honestly records the v2 schedule originated
from a v1 migration, distinguishable from a live fire AND
from ``schedule_created``. NO bespoke v1-audit-JSONL parser;
NO read of ``data/contract_audit/`` or
``contract_failures.jsonl`` (β REJECTED — silent
v1-internals extension; γ REJECTED — silent under-delivery;
``docs/PHASE_16_PLAN.md`` §9).

**v1 READ-ONLY (BINDING).** Reads v1 ONLY via the shipped
``ContractStore`` (``list_all`` / ``load_latest``) — through
the slice-1 ``mapper``. NEVER ``app.contracts.executor`` /
``app.tasks`` / ``app.scheduler_instance`` / any v1 write
path; NEVER ``data/contract_audit`` / ``contract_failures``.
ZERO v1 mutation.

**v2 write = the shipped storage spine.** ``with_fresh_hash``
→ ONE ``with transaction(conn): insert_schedule +
append_event(SCHEDULE_CREATED) +
append_event(MIGRATION_V1_TO_V2_COMPLETE)`` — both-or-neither
(the phase-14 ``_commit_success_atomic`` discipline: no
schedule without lineage, no lineage without schedule).
Composes ONLY shipped primitives — NO bespoke commit, NO SQL
reimpl, NO new DDL / v002. This is the same composition
``authoring/commit.py::schedule_draft_commit`` uses.

**GATED.** ``confirm`` defaults to ``False`` ⇒ a PURE dry-run
(ZERO write). ``confirm=True`` is the explicit write flag.
Admin-invoked; NOT auto-boot, NOT binding-wired.

**Idempotent.** Re-run ⇒ NO duplicate v2 schedule (a
``get_schedule`` precheck) AND NO duplicate lineage event (a
content-addressed ``migrated_from`` precheck composing the
shipped ``list_events_for_schedule`` read).

``event_id_factory`` / ``clock`` are injected (DI) — this
module imports no clock / id source (the phase-5 rule 10
carried 11→16).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §12 step 16
- ``docs/PHASE_16_PLAN.md`` §1 / §4 / §9
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable, Mapping

from app.contracts.store import ContractStore
from app.v2.enums import EventKind
from app.v2.migration.mapper import assess_contract, contract_to_schedule_spec
from app.v2.migration.results import (
    MigrationBinding,
    MigrationCommitEntry,
    MigrationCommitReport,
    MigrationOutcome,
    MigrationStatus,
)
from app.v2.models.event import Event
from app.v2.storage.events import append_event, list_events_for_schedule
from app.v2.storage.schedules import get_schedule, insert_schedule
from app.v2.storage.transactions import transaction


def _lineage_marker(contract_id: str, contract_hash: str) -> str:
    """The content-addressed migration provenance value."""
    return f"{contract_id}@{contract_hash}"


def _already_has_lineage(
    conn: sqlite3.Connection,
    schedule_id: str,
    marker: str,
) -> bool:
    """Content-addressed lineage precheck — True iff a prior
    ``migration_v1_to_v2_complete`` for this schedule already
    carries this exact ``migrated_from`` marker. Composes the
    shipped ``list_events_for_schedule`` read (no SQL reimpl)."""
    prior = list_events_for_schedule(
        conn,
        schedule_id,
        kind=EventKind.MIGRATION_V1_TO_V2_COMPLETE,
    )
    return any(
        e.payload.get("migrated_from") == marker for e in prior
    )


def migrate_contracts(
    *,
    store: ContractStore,
    conn: sqlite3.Connection,
    bindings: Mapping[str, MigrationBinding],
    event_id_factory: Callable[[], str],
    clock: Callable[[], datetime],
    confirm: bool = False,
) -> MigrationCommitReport:
    """Migrate every v1 contract that is structurally
    v2-expressible (slice-1 ``assess_contract``) AND has an
    operator-supplied :class:`MigrationBinding`.

    ``confirm=False`` (DEFAULT) ⇒ PURE dry-run: ZERO write.
    ``confirm=True`` ⇒ the gated write: per contract, ONE
    atomic transaction inserting the v2 schedule + its
    ``schedule_created`` + the ``migration_v1_to_v2_complete``
    lineage event (both-or-neither). Idempotent.
    """
    entries: list[MigrationCommitEntry] = []

    for contract_id in store.list_all():
        contract = store.load_latest(contract_id)

        assessment = assess_contract(contract)
        if assessment.status is MigrationStatus.SKIPPED:
            entries.append(
                MigrationCommitEntry(
                    contract_id=contract_id,
                    outcome=MigrationOutcome.SKIPPED_NOT_MIGRATABLE,
                    skip_reasons=assessment.skip_reasons,
                )
            )
            continue

        binding = bindings.get(contract_id)
        if binding is None:
            entries.append(
                MigrationCommitEntry(
                    contract_id=contract_id,
                    outcome=MigrationOutcome.SKIPPED_NO_BINDING,
                    skip_reasons=[
                        "no operator MigrationBinding supplied "
                        "(owner.platform / delivery.target_session_id "
                        "are operator-supplied, never fabricated)"
                    ],
                )
            )
            continue

        spec = contract_to_schedule_spec(
            contract, binding=binding
        ).with_fresh_hash()
        marker = _lineage_marker(contract.id, contract.hash)

        # Idempotency precheck (Q6): schedule-exists OR a prior
        # content-addressed lineage marker ⇒ skip — NO
        # duplicate schedule, NO duplicate lineage event.
        if get_schedule(conn, spec.id) is not None or (
            _already_has_lineage(conn, spec.id, marker)
        ):
            entries.append(
                MigrationCommitEntry(
                    contract_id=contract_id,
                    outcome=MigrationOutcome.SKIPPED_ALREADY_EXISTS,
                    schedule_id=spec.id,
                )
            )
            continue

        if not confirm:
            # Dry-run DEFAULT — PURE, ZERO write.
            entries.append(
                MigrationCommitEntry(
                    contract_id=contract_id,
                    outcome=MigrationOutcome.DRY_RUN_WOULD_MIGRATE,
                    schedule_id=spec.id,
                )
            )
            continue

        now = clock()
        created_event = Event(
            id=event_id_factory(),
            run_id=None,
            schedule_id=spec.id,
            ts=now,
            kind=EventKind.SCHEDULE_CREATED,
            payload={"hash": spec.hash, "template": None},
            correlates=None,
        )
        lineage_event = Event(
            id=event_id_factory(),
            run_id=None,
            schedule_id=spec.id,
            ts=now,
            kind=EventKind.MIGRATION_V1_TO_V2_COMPLETE,
            payload={
                "migrated_from": marker,
                "backfill": True,
                "v1_contract_id": contract.id,
                "v1_contract_hash": contract.hash,
            },
            correlates=created_event.id,
        )

        try:
            with transaction(conn):
                insert_schedule(conn, spec)
                append_event(conn, created_event)
                append_event(conn, lineage_event)
        except sqlite3.IntegrityError:
            # A racing/duplicate insert — idempotent-safe: the
            # whole TX rolled back (both-or-neither); report it
            # as already-exists rather than fail the pass.
            entries.append(
                MigrationCommitEntry(
                    contract_id=contract_id,
                    outcome=MigrationOutcome.SKIPPED_ALREADY_EXISTS,
                    schedule_id=spec.id,
                )
            )
            continue

        entries.append(
            MigrationCommitEntry(
                contract_id=contract_id,
                outcome=MigrationOutcome.MIGRATED,
                schedule_id=spec.id,
                lineage_event_id=lineage_event.id,
            )
        )

    migrated = sum(
        1 for e in entries if e.outcome is MigrationOutcome.MIGRATED
    )
    return MigrationCommitReport(
        dry_run=not confirm,
        entries=entries,
        migrated_count=migrated,
        skipped_count=len(entries) - migrated,
    )


__all__ = ["migrate_contracts"]
