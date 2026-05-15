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

from app.v2.authoring.commit import schedule_draft_commit
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
from app.v2.authoring.dry_run import schedule_dry_run
from app.v2.authoring.freeze import schedule_freeze
from app.v2.authoring.handshake import (
    DEFAULT_HANDSHAKE_BASE,
    DryRunMode,
    HandshakeRecord,
    HandshakeStore,
)
from app.v2.authoring.lifecycle import (
    schedule_archive,
    schedule_pause,
    schedule_resume,
    schedule_revive,
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
from app.v2.authoring.templates import (
    SCHEDULE_CREATE_REMINDER_TOOL_NAME,
    make_schedule_create_reminder,
)


__all__ = [
    "DEFAULT_DRAFT_BASE",
    "DEFAULT_HANDSHAKE_BASE",
    "DraftStore",
    "DryRunMode",
    "HandshakeRecord",
    "HandshakeStore",
    "SCHEDULE_CREATE_REMINDER_TOOL_NAME",
    "ScheduleSpecDraft",
    "ToolResponse",
    "ToolResponseStatus",
    "make_schedule_create_reminder",
    "schedule_archive",
    "schedule_draft_commit",
    "schedule_draft_compile",
    "schedule_draft_discard",
    "schedule_draft_list",
    "schedule_draft_start",
    "schedule_dry_run",
    "schedule_freeze",
    "schedule_pause",
    "schedule_resume",
    "schedule_revive",
    "schedule_set_cron",
    "schedule_set_delivery",
    "schedule_set_description",
    "schedule_set_failure_policy",
    "schedule_set_one_off",
    "schedule_set_owner",
]
