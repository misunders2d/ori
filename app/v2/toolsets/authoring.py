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

Tag matrix per §4 (phase-7 plan, extended by phase-8 slice 5):

- Draft setters (start / description / owner / cron /
  one_off / failure_policy): ``filesystem_write``.
- ``schedule_set_delivery``: ``read_external`` +
  ``uses_oauth`` + ``filesystem_write``.
- ``schedule_draft_compile``, ``schedule_draft_list``:
  ``filesystem_read`` (round-3 L785 new tag).
- ``schedule_draft_discard``: ``filesystem_write``.
- Lifecycle tools (pause / resume / archive / revive):
  ``db_write`` (round-3 L807 new tag).

Phase 8 slice 5 additions (per ``docs/PHASE_8_PLAN.md`` §4):

- ``schedule_dry_run``: ``filesystem_read`` +
  ``filesystem_write`` (reads draft; writes handshake).
- ``schedule_freeze``: ``filesystem_read`` (reads draft +
  handshake; no DB write, no file write).
- ``schedule_draft_commit``: ``db_write`` +
  ``filesystem_write`` (DB insert + deletes draft +
  handshake on success).
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

from app.v2.authoring.commit import schedule_draft_commit
from app.v2.authoring.compile import (
    schedule_draft_compile,
    schedule_draft_discard,
    schedule_draft_list,
)
from app.v2.authoring.delivery import schedule_set_delivery
from app.v2.authoring.dry_run import schedule_dry_run
from app.v2.authoring.freeze import schedule_freeze
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
from app.v2.authoring.responses import ToolResponse
from app.v2.authoring.templates import (
    SCHEDULE_CREATE_REMINDER_TOOL_NAME,
)
from app.v2.descriptors.tool import ToolDescriptor
from app.v2.registry import ToolRegistry
from app.v2.runtime import _owner_default
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
    # ----- Dry-run + freeze + commit (phase 8 slice 5) -----
    ToolDescriptor(
        name="schedule_dry_run",
        description=(
            "Validate the draft against the §5.5 chokepoint "
            "and record a 60-second dry-run handshake. "
            "validate_only is implemented (OneOff + "
            "source-driven cron); mocked_inputs / real "
            "(an authoring-time source-resolving dry-run) "
            "return mode_not_implemented pending the "
            "dry-run source-resolution path (a later step) "
            "— note source-driven schedules still resolve "
            "+ fire correctly at runtime via the worker."
        ),
        tags={
            ToolCapabilityTag.FILESYSTEM_READ,
            ToolCapabilityTag.FILESYSTEM_WRITE,
        },
        module=f"{_AUTHORING_MODULE}.dry_run",
    ),
    ToolDescriptor(
        name="schedule_freeze",
        description=(
            "Verify the dry-run handshake is fresh + "
            "hash-matches the current draft body and return "
            "the canonical spec. No DB or file mutation. "
            "OneOff (step 9) + cron (step 11, source-driven "
            "recurring series) are authorable; other "
            "trigger types are refused with "
            "trigger_type_pending_step_unlock until their "
            "own step."
        ),
        tags={ToolCapabilityTag.FILESYSTEM_READ},
        module=f"{_AUTHORING_MODULE}.freeze",
    ),
    ToolDescriptor(
        name="schedule_draft_commit",
        description=(
            "Atomic, single-transaction insert_schedule "
            "(+ insert_execution_plan for a source-driven "
            "draft — both-or-neither) + append_event("
            "schedule_created); on success best-effort "
            "delete of draft + handshake files (WARNING log "
            "on cleanup failure). OneOff + cron authorable."
        ),
        tags={
            ToolCapabilityTag.DB_WRITE,
            ToolCapabilityTag.FILESYSTEM_WRITE,
        },
        module=f"{_AUTHORING_MODULE}.commit",
    ),
    # ----- schedule_create_reminder (phase 9 slice 3) -----
    ToolDescriptor(
        name=SCHEDULE_CREATE_REMINDER_TOOL_NAME,
        description=(
            "Create a OneOff reminder via the v2 template "
            "pipeline. Wraps draft → dry_run → freeze → "
            "commit into one agent-facing call; returns "
            "ok(schedule_id, spec) on success."
        ),
        tags={
            ToolCapabilityTag.DB_WRITE,
            ToolCapabilityTag.FILESYSTEM_WRITE,
            ToolCapabilityTag.READ_EXTERNAL,
            ToolCapabilityTag.USES_OAUTH,
        },
        module=f"{_AUTHORING_MODULE}.templates",
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
# schedule_create_reminder default stub (phase 9 slice 3)
# ---------------------------------------------------------------------------


async def _stub_schedule_create_reminder(
    at: str,
    recipient_channel: str,
    text: str,
) -> ToolResponse:
    """Default stub when no production closure is bound to
    the :class:`AuthoringToolset`. Signature mirrors the
    production closure so the ADK ``FunctionTool`` schema
    stays stable regardless of whether the toolset is
    DI-wired. Raises :class:`NotImplementedError` if
    invoked — phase-9 cutover (slice 8) replaces the stub
    with the production closure built via
    :func:`app.v2.authoring.templates.make_schedule_create_reminder`.
    """
    raise NotImplementedError(
        "schedule_create_reminder is unmounted; phase-9 "
        "cutover binds the production closure via "
        "make_schedule_create_reminder(...)"
    )


_stub_schedule_create_reminder.__name__ = (
    SCHEDULE_CREATE_REMINDER_TOOL_NAME
)
_stub_schedule_create_reminder.__qualname__ = (
    SCHEDULE_CREATE_REMINDER_TOOL_NAME
)


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
        # Phase 9 slice 6 — `expected_owner_id` is optional;
        # when omitted, falls back to
        # `_owner_default.DEFAULT_AUTHORING_OWNER_ID`
        # (env var captured ONCE at import time per phase-9
        # plan §3.6). Precedence: explicit kwarg > env
        # fallback > startup error. Both None at construction
        # time raises RuntimeError so the bot refuses to
        # start instead of silently mounting against a
        # missing tenant id. Phase-7 round-2 L365 / Q10
        # close.
        expected_owner_id: Optional[str] = None,
        slack_client: Optional[object] = None,
        cache_base: Optional[object] = None,
        # Phase 9 slice 3: the schedule_create_reminder
        # production closure. Built via
        # ``app.v2.authoring.templates.make_schedule_create_reminder``
        # at agent mount time (phase-9 cutover slice 8). When
        # None, the toolset binds the
        # ``_stub_schedule_create_reminder`` closure (same
        # ``(at, recipient_channel, text)`` signature so the
        # FunctionTool schema is stable); the stub raises
        # NotImplementedError if invoked.
        schedule_create_reminder: Optional[
            Callable[[str, str, str], Awaitable[ToolResponse]]
        ] = None,
    ) -> None:
        # Resolve owner id: explicit kwarg wins; otherwise
        # consult the env-derived default (read via attribute
        # lookup on the module so tests can monkeypatch the
        # constant); both None → startup error.
        resolved = expected_owner_id
        if resolved is None:
            resolved = _owner_default.DEFAULT_AUTHORING_OWNER_ID
        if resolved is None:
            raise RuntimeError(
                "AuthoringToolset requires expected_owner_id: "
                "pass the kwarg explicitly OR set the "
                "V2_AUTHORING_OWNER_ID environment variable "
                "before importing this module. The constructor "
                "refuses to silently mount against a missing "
                "tenant id (phase-9 plan §3.6 / phase-7 round-2 "
                "reviewer L365)."
            )
        # ADK BaseToolset has no documented __init__ args we
        # need to forward; the metadata is constructor-time
        # only for phase-7 hygiene.
        self._expected_owner_id = resolved
        self._slack_client = slack_client
        self._cache_base = cache_base
        self._schedule_create_reminder = (
            schedule_create_reminder
            if schedule_create_reminder is not None
            else _stub_schedule_create_reminder
        )

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
            FunctionTool(func=schedule_dry_run),
            FunctionTool(func=schedule_freeze),
            FunctionTool(func=schedule_draft_commit),
            FunctionTool(func=self._schedule_create_reminder),
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
