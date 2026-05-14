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
