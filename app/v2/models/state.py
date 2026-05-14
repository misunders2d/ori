"""V2 scheduler — ScheduleState model.

Cross-fire state for series-style ScheduleSpecs. Persisted in
the ``schedule_state`` SQLite table with a row per
``(schedule_id, key)``. Writes use compare-and-swap (CAS) via
the ``version`` column — see design contract §6.5.

LLM never touches ScheduleState directly. State is produced by
deterministic loaders (e.g. ``pick_by_day`` reading + bumping a
``last_fired_day`` counter) and consumed by emit adapters.

See ``docs/CONTRACTS_V2_DESIGN.md`` §4.7 + ``docs/PHASE_1_PLAN.md``
§4.8.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ScheduleState(BaseModel):
    """A single key/value entry in a ScheduleSpec's per-schedule
    state namespace.

    The ``version`` field is the CAS token: writers compare it
    against the value they read and refuse the write if the
    on-disk version has advanced underneath them. Read-modify-
    write loops thus serialise without external locking beyond
    the SQLite single-writer.
    """

    model_config = ConfigDict(extra="forbid")

    schedule_id: str
    key: str
    value: Any = Field(
        description="JSON-serialisable value. Strings, numbers, "
        "booleans, lists, and dicts are first-class. Larger or "
        "non-JSON payloads should be referenced by hash + stored "
        "alongside in the snapshot table.",
    )
    version: int = Field(
        default=1,
        ge=1,
        description="Bumped by one on each successful write. "
        "Starts at 1 for the first persisted value.",
    )
    written_at: datetime
    written_by_run: Optional[str] = Field(
        default=None,
        description="Run id that produced this value. None for "
        "author-time seeds (e.g. initial syllabus snapshot at "
        "freeze time).",
    )
