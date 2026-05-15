"""V2 scheduler — authoring tools package.

Phase 7 ships the typed ADK authoring surface that builds
ScheduleSpec drafts via tool calls per design §5.1.

Slice 1 surface: ToolResponse discriminated union + draft
storage. Later slices add setters, delivery resolver wiring,
compile/discard/list, and lifecycle tools. The package
``__init__`` is finalised in slice 6 alongside the toolset
bundle.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.1, §5.5
- ``docs/PHASE_7_PLAN.md``
"""

from app.v2.authoring.compile import (
    schedule_draft_compile,
    schedule_draft_discard,
    schedule_draft_list,
)
from app.v2.authoring.delivery import schedule_set_delivery
from app.v2.authoring.drafts import (
    DEFAULT_DRAFT_BASE,
    DraftStore,
    ScheduleSpecDraft,
)
from app.v2.authoring.responses import ToolResponse, ToolResponseStatus
from app.v2.authoring.setters import (
    schedule_draft_start,
    schedule_set_cron,
    schedule_set_description,
    schedule_set_failure_policy,
    schedule_set_one_off,
    schedule_set_owner,
)


__all__ = [
    "DEFAULT_DRAFT_BASE",
    "DraftStore",
    "ScheduleSpecDraft",
    "ToolResponse",
    "ToolResponseStatus",
    "schedule_draft_compile",
    "schedule_draft_discard",
    "schedule_draft_list",
    "schedule_draft_start",
    "schedule_set_cron",
    "schedule_set_delivery",
    "schedule_set_description",
    "schedule_set_failure_policy",
    "schedule_set_one_off",
    "schedule_set_owner",
]
