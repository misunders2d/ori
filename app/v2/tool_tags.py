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
    """The seven canonical capability tags.

    Adding a new tag is a deliberate act — every policy helper
    in this module + every guardrail consuming the tag set must
    be updated in the same change. The drift-guard test in
    ``tests/v2/test_tool_tags.py`` iterates this enum and
    asserts each value is reachable; a new value that doesn't
    fail the test indicates the helper coverage is also
    extended.
    """

    READ_EXTERNAL = "read_external"
    WRITE_EXTERNAL = "write_external"
    SEND_MESSAGE = "send_message"
    FILESYSTEM_WRITE = "filesystem_write"
    PRIVILEGED = "privileged"
    COSTLY = "costly"
    USES_OAUTH = "uses_oauth"


# Tags that a reasoning step with ``tool_mode=read_only`` (the
# default) is forbidden from invoking — they all imply a
# side-effecting operation, which must be routed through an emit
# step instead.
_READ_ONLY_BLOCKING_TAGS: frozenset[ToolCapabilityTag] = frozenset(
    {
        ToolCapabilityTag.WRITE_EXTERNAL,
        ToolCapabilityTag.SEND_MESSAGE,
        ToolCapabilityTag.FILESYSTEM_WRITE,
        ToolCapabilityTag.PRIVILEGED,
    }
)


def is_blocked_by_read_only_reasoning(
    tags: AbstractSet[ToolCapabilityTag],
) -> bool:
    """True iff a read-only reasoning step is forbidden from
    invoking a tool carrying these tags.

    The guard fires on overlap with any of
    ``write_external``, ``send_message``, ``filesystem_write``,
    or ``privileged``. ``costly`` and ``uses_oauth`` are not
    blocking by themselves — they're observational tags that
    other helpers consume (cost warnings, OAuth flow setup).
    """
    return bool(set(tags) & _READ_ONLY_BLOCKING_TAGS)


def requires_admin_approval(tags: AbstractSet[ToolCapabilityTag]) -> bool:
    """True iff the tool needs explicit admin approval at
    authoring time (CustomFlow friction trigger; see design
    §5.9)."""
    return ToolCapabilityTag.PRIVILEGED in tags


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
