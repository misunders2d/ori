"""V2 scheduler — Trigger discriminator union.

Phase 1 declares all five subclasses so phase-3 work can land
without reshuffling the union. Only ``cron`` and ``one_off``
carry non-trivial validators in phase 1; ``interval``,
``event``, and ``conditional`` are shape-only and will gain
their validators when their wakeup / gate evaluation logic
lands (phase 3+).

The discriminator field is ``type``. Pydantic v2's
``discriminator=`` Annotated form lets the union deserialize
deterministically from JSON without ambiguity.

See ``docs/CONTRACTS_V2_DESIGN.md`` §4.2 + ``docs/PHASE_1_PLAN.md``
§4.2.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# CronTrigger — recurring schedule via 5-field cron expression
# ---------------------------------------------------------------------------


class CronTrigger(BaseModel):
    """Recurring schedule expressed as a standard 5-field cron
    string interpreted in ``timezone`` (IANA name).

    Phase 1 validates only:
      - ``cron`` has exactly 5 whitespace-separated fields.
      - ``timezone`` is non-empty.

    Full cron-expression parsing (range / step / list syntax)
    is deferred to the wakeup function at phase 4 — APScheduler
    will reject invalid expressions at that point.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["cron"] = "cron"
    cron: str = Field(
        description="Standard 5-field cron expression — e.g. "
        "'0 18 * * MON-FRI'. Day-of-week MUST use 3-letter "
        "names; numeric DOW is rejected at the wakeup layer "
        "because APScheduler's from_crontab uses non-standard "
        "0=Monday.",
    )
    timezone: str = Field(
        default="UTC",
        description="IANA timezone name (e.g. 'Europe/Kyiv', "
        "'America/Los_Angeles', 'UTC').",
    )

    @field_validator("cron")
    @classmethod
    def _cron_has_five_fields(cls, v: str) -> str:
        v = v.strip()
        fields = v.split()
        if len(fields) != 5:
            raise ValueError(
                f"cron must have exactly 5 whitespace-separated "
                f"fields (minute hour day month dow); got "
                f"{len(fields)}: {v!r}"
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _timezone_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("timezone must be non-empty")
        return v


# ---------------------------------------------------------------------------
# OneOffTrigger — fire once at a specific datetime
# ---------------------------------------------------------------------------


class OneOffTrigger(BaseModel):
    """Fire once at ``at_iso_datetime``. The wakeup function
    auto-unschedules after the fire.

    Pydantic v2's native datetime parser accepts ISO 8601
    strings. ``timezone`` is included separately so the
    contract body carries the author's intent even when the
    datetime serializes to UTC.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["one_off"] = "one_off"
    at_iso_datetime: datetime
    timezone: str = Field(
        default="UTC",
        description="IANA timezone name used to interpret + display "
        "the firing time. The stored ``at_iso_datetime`` is always "
        "rendered to UTC for the wakeup layer.",
    )

    @field_validator("timezone")
    @classmethod
    def _timezone_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("timezone must be non-empty")
        return v


# ---------------------------------------------------------------------------
# IntervalTrigger — phase-3 stub
# ---------------------------------------------------------------------------


class IntervalTrigger(BaseModel):
    """Recurring every ``every_seconds`` seconds.

    Phase-3 placeholder — class declared so the Trigger union
    is complete; wakeup-callback support lands in phase 3+.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["interval"] = "interval"
    every_seconds: int = Field(ge=1)


# ---------------------------------------------------------------------------
# EventTrigger — phase-3 stub
# ---------------------------------------------------------------------------


class EventTrigger(BaseModel):
    """Fire when the named event is published on the internal
    event bus.

    Phase-3 placeholder; the event-bus wiring + push-notification
    receivers land in phase 3+.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["event"] = "event"
    event: str


# ---------------------------------------------------------------------------
# ConditionalTrigger — phase-3 stub
# ---------------------------------------------------------------------------


class ConditionalTrigger(BaseModel):
    """Poll a registered ``gate`` predicate every
    ``poll_seconds`` seconds; fire when the gate returns True.

    Phase-3 placeholder.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["conditional"] = "conditional"
    gate: str
    poll_seconds: int = Field(ge=1)


# ---------------------------------------------------------------------------
# Trigger union — discriminated by ``type`` field
# ---------------------------------------------------------------------------


Trigger = Annotated[
    Union[
        CronTrigger,
        OneOffTrigger,
        IntervalTrigger,
        EventTrigger,
        ConditionalTrigger,
    ],
    Field(discriminator="type"),
]
