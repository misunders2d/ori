"""Canonical tool capability tags + pure policy helpers.

Implements ``docs/CONTRACTS_V2_DESIGN.md`` §5.4 — every tool /
source loader / emit adapter the agent may invoke carries one or
more of these tags. The runtime guard composes them with the
calling step's ``tool_mode`` to decide whether the call is
allowed.

This module is pure contract: enums + Pydantic descriptors live
here, plus helper functions over a tag set. No I/O, no adapter
implementation. Phase 2 is the metadata surface — runtime
composition lands in a later phase.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.4 (tool metadata tags)
- ``docs/PHASE_2_PLAN.md`` §3
"""

from __future__ import annotations

from enum import Enum
from typing import AbstractSet


class ToolCapabilityTag(str, Enum):
    """The nine canonical capability tags.

    Adding a new tag is a deliberate act — every policy helper
    in this module + every guardrail consuming the tag set must
    be updated in the same change. The drift-guard test in
    ``tests/v2/test_tool_tags.py`` iterates this enum and
    asserts each value is reachable; a new value that doesn't
    fail the test indicates the helper coverage is also
    extended.

    Phase 7 slice 6 added ``FILESYSTEM_READ`` and ``DB_WRITE``
    per design §5.4 (round-3 reviewer L785 + L807):

    - ``FILESYSTEM_READ`` — local-file reads the bot owns
      (draft JSON, cached registry, config). Distinct from
      ``READ_EXTERNAL`` (third-party API reads). NOT blocking
      under read-only reasoning.
    - ``DB_WRITE`` — mutates the v2 SQLite store. Distinct
      from ``FILESYSTEM_WRITE`` because the SQLite path is
      internal state the v2 runtime owns; admin-approval
      gating differs. **Blocking** under read-only
      reasoning so a future reasoning step with
      ``tool_mode=read_only`` cannot mutate the v2 store.
    """

    READ_EXTERNAL = "read_external"
    WRITE_EXTERNAL = "write_external"
    SEND_MESSAGE = "send_message"
    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    DB_WRITE = "db_write"
    PRIVILEGED = "privileged"
    COSTLY = "costly"
    USES_OAUTH = "uses_oauth"


# Tags that a reasoning step with ``tool_mode=read_only`` (the
# default) is forbidden from invoking — they all imply a
# side-effecting operation, which must be routed through an emit
# step instead. Phase 7 slice 6 extended this set with
# ``DB_WRITE`` (round-3 reviewer L807); ``FILESYSTEM_READ`` is
# explicitly NOT in this set — local introspection is safe under
# read-only reasoning.
_READ_ONLY_BLOCKING_TAGS: frozenset[ToolCapabilityTag] = frozenset(
    {
        ToolCapabilityTag.WRITE_EXTERNAL,
        ToolCapabilityTag.SEND_MESSAGE,
        ToolCapabilityTag.FILESYSTEM_WRITE,
        ToolCapabilityTag.DB_WRITE,
        ToolCapabilityTag.PRIVILEGED,
    }
)


# Tags that, when present on a tool a CustomFlow reasoning
# step references, raise the §5.9 advisory friction SIGNAL
# (the warning-severity ``customflow_admin_approval_advisory``
# validation issue — phase-12 slice-3 / Option A). SIGNAL
# only: there is NO admin-approval gate or executor — the
# §5.9 gate stays conceptual and its consuming flow is
# DEFERRED (no §12 step owns it). The set matches the "Tools
# tagged privileged or costly" + "filesystem_write tools"
# bullets in design §5.9.
_ADMIN_APPROVAL_TAGS: frozenset[ToolCapabilityTag] = frozenset(
    {
        ToolCapabilityTag.PRIVILEGED,
        ToolCapabilityTag.COSTLY,
        ToolCapabilityTag.FILESYSTEM_WRITE,
    }
)


def is_blocked_by_read_only_reasoning(
    tags: AbstractSet[ToolCapabilityTag],
) -> bool:
    """True iff a read-only reasoning step is forbidden from
    invoking a tool carrying these tags.

    The guard fires on overlap with any of
    ``write_external``, ``send_message``, ``filesystem_write``,
    ``db_write``, or ``privileged``. ``costly``,
    ``uses_oauth``, ``read_external``, and ``filesystem_read``
    are not blocking by themselves — they're observational
    tags that other helpers consume (cost warnings, OAuth flow
    setup) or designate a read that does not mutate state.
    """
    return bool(set(tags) & _READ_ONLY_BLOCKING_TAGS)


def requires_admin_approval(tags: AbstractSet[ToolCapabilityTag]) -> bool:
    """True iff the tool needs explicit admin approval at
    authoring time (CustomFlow friction trigger).

    Per design §5.9, any of the following tags trips the gate:

    - ``PRIVILEGED`` — admin-only ops.
    - ``COSTLY`` — billable destinations (BQ scans, LLM calls);
      admin sign-off prevents unbounded spend.
    - ``FILESYSTEM_WRITE`` — local filesystem mutation needs
      a human in the loop because the bot's deploy host is
      the same machine the agent reasons on.
    """
    return bool(set(tags) & _ADMIN_APPROVAL_TAGS)


def is_costly(tags: AbstractSet[ToolCapabilityTag]) -> bool:
    """True iff the tool's invocation has measurable financial
    cost (BigQuery scans, LLM calls). Triggers a cost warning
    at authoring time."""
    return ToolCapabilityTag.COSTLY in tags


def requires_oauth(tags: AbstractSet[ToolCapabilityTag]) -> bool:
    """True iff the tool consumes the user's OAuth credentials.
    The author flow surfaces an OAuth consent reminder when
    this is True for any tool in a draft schedule."""
    return ToolCapabilityTag.USES_OAUTH in tags


def is_user_facing(tags: AbstractSet[ToolCapabilityTag]) -> bool:
    """True iff the tool delivers content to a human user
    (Slack post, Telegram DM, email). Drives the
    delivery-fallback chain selection."""
    return ToolCapabilityTag.SEND_MESSAGE in tags


__all__ = [
    "ToolCapabilityTag",
    "is_blocked_by_read_only_reasoning",
    "requires_admin_approval",
    "is_costly",
    "requires_oauth",
    "is_user_facing",
]
