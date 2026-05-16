"""V2 scheduler — Event model (the EventLedger row shape).

The EventLedger is the single source of audit truth. Every state
transition writes an Event row. Observability queries (failure
monitor, status tool, replay) read against the ``events`` SQLite
table.

Phase 1 keeps ``payload`` as a free-form ``dict``. Phase 2
introduces per-EventKind Pydantic payload models so payloads can
be type-checked alongside the kind discriminator. The phase-1
boundary is intentional — model the shape now, tighten payload
schemas alongside the runtime that produces them.

Phase 11 slice 6 advances that boundary for ONE kind by
reviewer directive: :class:`RunSucceededPayload` is the first
per-EventKind payload model. It carries a TYPED, ADDITIVE,
DEFAULTED ``skipped_unchanged`` discriminator so a consumer
can deterministically tell a success-by-delivering apart from
a ``skip_unchanged`` no-op success WITHOUT a loose ad-hoc
``payload[...]`` dict key (the Option-B fork-resolution for
``RecurringSeriesFromSource``: no v002 events-schema migration
in a cutover phase — a named skip event-kind, if ever needed,
belongs with the step-14 idempotency event-schema work, and is
explicitly NOT ``emit_skipped_idempotent`` which stays reserved
for idempotency dedup, not content-unchanged).

See ``docs/CONTRACTS_V2_DESIGN.md`` §4.0 + ``docs/PHASE_1_PLAN.md``
§4.7.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.v2.enums import EventKind


class Event(BaseModel):
    """A single EventLedger row. Append-only — no UPDATE in the
    runtime; corrections happen by writing a new event that
    correlates to the prior one.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description="UUID4 string. Caller-generated so the "
        "EventLedger row can be inserted in the same transaction "
        "as the state change it describes.",
    )
    run_id: Optional[str] = Field(
        default=None,
        description="The Run this event belongs to. None for "
        "schedule-level events like ``schedule_created`` or "
        "``schedule_archived``.",
    )
    schedule_id: str
    ts: datetime = Field(
        description="UTC timestamp when the event was emitted.",
    )
    kind: EventKind
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Event-specific structured data. Phase-1 "
        "keeps this open; phase-2 introduces per-kind Pydantic "
        "models so payload shape can be enforced.",
    )
    correlates: Optional[str] = Field(
        default=None,
        description="Optional id of a related event. Common "
        "uses: ``admin_alert_acked.correlates = "
        "admin_alert_sent.id``, ``run_retry_scheduled.correlates "
        "= run_failed.id``, ``schedule_revived.correlates = "
        "schedule_archived.id``.",
    )


class RunSucceededPayload(BaseModel):
    """Typed payload for :attr:`EventKind.RUN_SUCCEEDED` —
    the first per-EventKind payload model (phase-11 slice 6,
    reviewer-directed; see the module docstring).

    ``skipped_unchanged`` is the Option-B discriminator: a
    real delivery leaves it ``False`` (the default — every
    pre-slice-6 RUN_SUCCEEDED producer/consumer is
    semantically unaffected: the field is ADDITIVE and
    defaulted, mirroring the slice-4
    ``ResolveOutcome.changed_vs_prior`` additive
    discipline), a ``RecurringSeriesFromSource``
    ``skip_unchanged`` no-op success sets it ``True``. The
    skip rides the EXISTING single ``running → succeeded``
    transition + its one ``RUN_SUCCEEDED`` write — no
    second event, no second transaction, no new event kind.

    The worker builds this model and ``model_dump()``s it
    into :attr:`Event.payload` (which stays a free-form
    dict at the storage boundary — the typing is enforced
    at the construction site, not the column)."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str
    skipped_unchanged: bool = False


__all__ = ["Event", "RunSucceededPayload"]
