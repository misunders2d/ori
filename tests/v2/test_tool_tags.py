"""Tests for ``app.v2.tool_tags``.

Pins per ``docs/PHASE_2_PLAN.md`` §5.1:

- Every canonical tag from design §5.4 is exposed by the enum.
- Unknown tag values rejected (Pydantic via descriptor +
  direct enum lookup).
- Drift guard: each enum value's string equals its lowercased
  Python identifier — protects against accidental rename.
- Each policy helper covers its canonical input set + a
  negative case.
- Empty tag set: every helper returns False.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.4
"""

from __future__ import annotations

import pytest

from app.v2.tool_tags import (
    ToolCapabilityTag,
    is_blocked_by_read_only_reasoning,
    is_costly,
    is_user_facing,
    requires_admin_approval,
    requires_oauth,
)


_CANONICAL_VALUES = {
    "read_external",
    "write_external",
    "send_message",
    "filesystem_write",
    "privileged",
    "costly",
    "uses_oauth",
}


# ---------------------------------------------------------------------------
# Enum coverage
# ---------------------------------------------------------------------------


def test_enum_exposes_all_canonical_values():
    """Drift guard: phase-2 plan lists exactly seven tags. The
    enum should expose precisely that set — no surprise
    additions, no omissions."""
    actual = {tag.value for tag in ToolCapabilityTag}
    assert actual == _CANONICAL_VALUES


@pytest.mark.parametrize(
    "value",
    sorted(_CANONICAL_VALUES),
)
def test_each_canonical_value_is_constructible(value):
    tag = ToolCapabilityTag(value)
    assert tag.value == value


@pytest.mark.parametrize(
    "bad_value",
    [
        "READ_EXTERNAL",       # wrong case
        "read-external",       # kebab
        "readexternal",        # squashed
        "read_external ",      # trailing space
        "",                    # empty
        "made_up_tag",         # not a tag
    ],
)
def test_unknown_value_rejected(bad_value):
    with pytest.raises(ValueError):
        ToolCapabilityTag(bad_value)


def test_enum_value_matches_identifier_lowercase():
    """Drift guard: every enum value string is exactly its
    Python identifier lowercased. Catches an accidental rename
    that diverges the name from the wire value."""
    for tag in ToolCapabilityTag:
        assert tag.value == tag.name.lower()


# ---------------------------------------------------------------------------
# is_blocked_by_read_only_reasoning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tag",
    [
        ToolCapabilityTag.WRITE_EXTERNAL,
        ToolCapabilityTag.SEND_MESSAGE,
        ToolCapabilityTag.FILESYSTEM_WRITE,
        ToolCapabilityTag.PRIVILEGED,
    ],
)
def test_read_only_blocks_each_write_side_tag(tag):
    assert is_blocked_by_read_only_reasoning({tag}) is True


@pytest.mark.parametrize(
    "tag",
    [
        ToolCapabilityTag.READ_EXTERNAL,
        ToolCapabilityTag.COSTLY,
        ToolCapabilityTag.USES_OAUTH,
    ],
)
def test_read_only_does_not_block_observational_or_read_tags(tag):
    assert is_blocked_by_read_only_reasoning({tag}) is False


def test_read_only_block_triggers_on_mixed_set():
    """A mixed set with one offending tag still blocks."""
    tags = {
        ToolCapabilityTag.READ_EXTERNAL,
        ToolCapabilityTag.SEND_MESSAGE,
    }
    assert is_blocked_by_read_only_reasoning(tags) is True


def test_read_only_block_returns_false_on_empty_set():
    assert is_blocked_by_read_only_reasoning(set()) is False


# ---------------------------------------------------------------------------
# requires_admin_approval
# ---------------------------------------------------------------------------


def test_requires_admin_approval_true_when_privileged_present():
    assert requires_admin_approval({ToolCapabilityTag.PRIVILEGED}) is True


def test_requires_admin_approval_false_without_privileged():
    assert (
        requires_admin_approval(
            {
                ToolCapabilityTag.READ_EXTERNAL,
                ToolCapabilityTag.WRITE_EXTERNAL,
                ToolCapabilityTag.COSTLY,
            }
        )
        is False
    )


def test_requires_admin_approval_false_on_empty_set():
    assert requires_admin_approval(set()) is False


# ---------------------------------------------------------------------------
# is_costly
# ---------------------------------------------------------------------------


def test_is_costly_true_when_costly_present():
    assert is_costly({ToolCapabilityTag.COSTLY}) is True


def test_is_costly_false_without_costly():
    assert is_costly({ToolCapabilityTag.READ_EXTERNAL}) is False


def test_is_costly_false_on_empty_set():
    assert is_costly(set()) is False


# ---------------------------------------------------------------------------
# requires_oauth
# ---------------------------------------------------------------------------


def test_requires_oauth_true_when_uses_oauth_present():
    assert requires_oauth({ToolCapabilityTag.USES_OAUTH}) is True


def test_requires_oauth_false_without_uses_oauth():
    assert requires_oauth({ToolCapabilityTag.READ_EXTERNAL}) is False


def test_requires_oauth_false_on_empty_set():
    assert requires_oauth(set()) is False


# ---------------------------------------------------------------------------
# is_user_facing
# ---------------------------------------------------------------------------


def test_is_user_facing_true_when_send_message_present():
    assert is_user_facing({ToolCapabilityTag.SEND_MESSAGE}) is True


def test_is_user_facing_false_without_send_message():
    """A write to external state isn't necessarily user-facing
    (e.g. sheets append). The helper must distinguish."""
    assert is_user_facing({ToolCapabilityTag.WRITE_EXTERNAL}) is False


def test_is_user_facing_false_on_empty_set():
    assert is_user_facing(set()) is False
