"""V2 scheduler — authoring toolset bundle.

Phase 7 slice 6 per ``docs/PHASE_7_PLAN.md`` §3.7 + §5.7.

Bundles every phase-7 authoring + lifecycle tool into one
ADK ``BaseToolset``. Two surfaces:

- :class:`AuthoringToolset` — ADK toolset. ``get_tools``
  returns ``FunctionTool`` instances for every authoring
  tool. NOT mounted on any agent in phase 7 (phase-9
  cutover does the binding).
- :data:`AUTHORING_TOOL_DESCRIPTORS` — list of
  :class:`ToolDescriptor` records keyed by tool name. Each
  carries the exact §5.4 tag set per the phase-7 plan §4
  matrix (round-3 reviewer L676). Slice 6 also registers
  these via :func:`register_descriptors` on a
  :class:`ToolRegistry`.

Tag matrix per §4 (phase-7 plan):

- Draft setters (start / description / owner / cron /
  one_off / failure_policy): ``filesystem_write``.
- ``schedule_set_delivery``: ``read_external`` +
  ``uses_oauth`` + ``filesystem_write``.
- ``schedule_draft_compile``, ``schedule_draft_list``:
  ``filesystem_read`` (round-3 L785 new tag).
- ``schedule_draft_discard``: ``filesystem_write``.
- Lifecycle tools (pause / resume / archive / revive):
  ``db_write`` (round-3 L807 new tag).
"""

from __future__ import annotations

from typing import Optional

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

from app.v2.authoring.compile import (
    schedule_draft_compile,
    schedule_draft_discard,
    schedule_draft_list,
)
from app.v2.authoring.delivery import schedule_set_delivery
from app.v2.authoring.lifecycle import (
    schedule_archive,
    schedule_pause,
    schedule_resume,
    schedule_revive,
)
from app.v2.authoring.setters import (
    schedule_draft_start,
    schedule_set_cron,
    schedule_set_description,
    schedule_set_failure_policy,
    schedule_set_one_off,
    schedule_set_owner,
)
from app.v2.descriptors.tool import ToolDescriptor
from app.v2.registry import ToolRegistry
from app.v2.tool_tags import ToolCapabilityTag


# ---------------------------------------------------------------------------
# Static descriptor table — §4 tag matrix
# ---------------------------------------------------------------------------


_AUTHORING_MODULE = "app.v2.authoring"


AUTHORING_TOOL_DESCRIPTORS: list[ToolDescriptor] = [
    # ----- Draft setters (filesystem_write) -----
    ToolDescriptor(
        name="schedule_draft_start",
        description="Create a new ScheduleSpec draft file.",
        tags={ToolCapabilityTag.FILESYSTEM_WRITE},
        module=f"{_AUTHORING_MODULE}.setters",
    ),
    ToolDescriptor(
        name="schedule_set_description",
        description=(
            "Set the description field on an existing draft."
        ),
        tags={ToolCapabilityTag.FILESYSTEM_WRITE},
        module=f"{_AUTHORING_MODULE}.setters",
    ),
    ToolDescriptor(
        name="schedule_set_owner",
        description="Set the owner UserRef on an existing draft.",
        tags={ToolCapabilityTag.FILESYSTEM_WRITE},
        module=f"{_AUTHORING_MODULE}.setters",
    ),
    ToolDescriptor(
        name="schedule_set_cron",
        description=(
            "Set a CronTrigger on an existing draft "
            "(numeric DOW rejected by phase-5 cron_guard)."
        ),
        tags={ToolCapabilityTag.FILESYSTEM_WRITE},
        module=f"{_AUTHORING_MODULE}.setters",
    ),
    ToolDescriptor(
        name="schedule_set_one_off",
        description=(
            "Set a OneOffTrigger on an existing draft "
            "(naive datetime rejected)."
        ),
        tags={ToolCapabilityTag.FILESYSTEM_WRITE},
        module=f"{_AUTHORING_MODULE}.setters",
    ),
    ToolDescriptor(
        name="schedule_set_failure_policy",
        description=(
            "Set the failure-handling policy on an existing draft."
        ),
        tags={ToolCapabilityTag.FILESYSTEM_WRITE},
        module=f"{_AUTHORING_MODULE}.setters",
    ),
    # ----- Delivery (read_external + uses_oauth + filesystem_write) -----
    ToolDescriptor(
        name="schedule_set_delivery",
        description=(
            "Resolve a Slack channel via the registry cache "
            "and set delivery on the draft. May trigger a "
            "Slack API call when the cache is absent."
        ),
        tags={
            ToolCapabilityTag.READ_EXTERNAL,
            ToolCapabilityTag.USES_OAUTH,
            ToolCapabilityTag.FILESYSTEM_WRITE,
        },
        module=f"{_AUTHORING_MODULE}.delivery",
    ),
    # ----- Compile / list (filesystem_read) -----
    ToolDescriptor(
        name="schedule_draft_compile",
        description=(
            "Validate the draft against validate_schedule_spec "
            "and return its canonical body. Read-only."
        ),
        tags={ToolCapabilityTag.FILESYSTEM_READ},
        module=f"{_AUTHORING_MODULE}.compile",
    ),
    ToolDescriptor(
        name="schedule_draft_list",
        description=(
            "List draft ids for the current session in "
            "lexicographic order."
        ),
        tags={ToolCapabilityTag.FILESYSTEM_READ},
        module=f"{_AUTHORING_MODULE}.compile",
    ),
    # ----- Discard (filesystem_write) -----
    ToolDescriptor(
        name="schedule_draft_discard",
        description="Delete a draft file. Idempotent.",
        tags={ToolCapabilityTag.FILESYSTEM_WRITE},
        module=f"{_AUTHORING_MODULE}.compile",
    ),
    # ----- Lifecycle (db_write) -----
    ToolDescriptor(
        name="schedule_pause",
        description=(
            "Flip a schedule's status to paused; appends "
            "schedule_paused to the EventLedger atomically."
        ),
        tags={ToolCapabilityTag.DB_WRITE},
        module=f"{_AUTHORING_MODULE}.lifecycle",
    ),
    ToolDescriptor(
        name="schedule_resume",
        description=(
            "Flip a paused schedule to active. Refuses when "
            "archived (admin re-approves via schedule_revive "
            "first per §11.4)."
        ),
        tags={ToolCapabilityTag.DB_WRITE},
        module=f"{_AUTHORING_MODULE}.lifecycle",
    ),
    ToolDescriptor(
        name="schedule_archive",
        description=(
            "Archive a schedule and cancel every pending Run "
            "in the same transaction."
        ),
        tags={ToolCapabilityTag.DB_WRITE},
        module=f"{_AUTHORING_MODULE}.lifecycle",
    ),
    ToolDescriptor(
        name="schedule_revive",
        description=(
            "Flip an archived schedule to PAUSED (not active "
            "— two-step gate per §11.4)."
        ),
        tags={ToolCapabilityTag.DB_WRITE},
        module=f"{_AUTHORING_MODULE}.lifecycle",
    ),
]


def register_descriptors(registry: ToolRegistry) -> None:
    """Register every authoring tool's descriptor on
    ``registry``. Metadata-only — does NOT wire the tools
    into any agent (phase 9 cutover does that)."""
    for descriptor in AUTHORING_TOOL_DESCRIPTORS:
        registry.register(descriptor)


# ---------------------------------------------------------------------------
# AuthoringToolset — ADK BaseToolset
# ---------------------------------------------------------------------------


class AuthoringToolset(BaseToolset):
    """ADK toolset that bundles every phase-7 authoring +
    lifecycle tool as a ``FunctionTool``.

    The toolset is **NOT registered with any agent** in
    phase 7. Tests instantiate it directly; production
    mounting happens at phase 9 cutover.

    The DI'd kwargs that each tool needs (store / clock /
    event_id_factory / slack_client / expected_owner_id /
    conn) are not threaded through the FunctionTool wrappers
    in phase 7 — the agent layer at phase 9 will supply them
    via state context. Phase 7 keeps the toolset as a
    **structural** bundle: a list of FunctionTool instances
    the agent will discover at mount time.
    """

    def __init__(
        self,
        *,
        # Round-3 reviewer L365 / Q10 — all DI required, no
        # env-derived defaults. These are accepted on the
        # constructor so phase-9 production wiring can supply
        # the real values; tests instantiate with stubs.
        expected_owner_id: str,
        slack_client: Optional[object] = None,
        cache_base: Optional[object] = None,
    ) -> None:
        # ADK BaseToolset has no documented __init__ args we
        # need to forward; the metadata is constructor-time
        # only for phase-7 hygiene.
        self._expected_owner_id = expected_owner_id
        self._slack_client = slack_client
        self._cache_base = cache_base

    async def get_tools(self, readonly_context=None):
        """Return every authoring + lifecycle tool as a
        :class:`FunctionTool`. Order matches the §4 tag
        matrix declaration order so the registry sees the
        same shape."""
        return [
            FunctionTool(func=schedule_draft_start),
            FunctionTool(func=schedule_set_description),
            FunctionTool(func=schedule_set_owner),
            FunctionTool(func=schedule_set_cron),
            FunctionTool(func=schedule_set_one_off),
            FunctionTool(func=schedule_set_failure_policy),
            FunctionTool(func=schedule_set_delivery),
            FunctionTool(func=schedule_draft_compile),
            FunctionTool(func=schedule_draft_list),
            FunctionTool(func=schedule_draft_discard),
            FunctionTool(func=schedule_pause),
            FunctionTool(func=schedule_resume),
            FunctionTool(func=schedule_archive),
            FunctionTool(func=schedule_revive),
        ]


__all__ = [
    "AUTHORING_TOOL_DESCRIPTORS",
    "AuthoringToolset",
    "register_descriptors",
]
