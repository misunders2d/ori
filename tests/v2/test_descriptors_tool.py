"""Tests for ``app.v2.descriptors.tool.ToolDescriptor``.

Pins per ``docs/PHASE_2_PLAN.md`` §5.2:

- Snake_case ``name`` validator (reject CamelCase, kebab, empty).
- ``description`` min length 8.
- ``tags`` non-empty.
- Unknown tag rejected (Pydantic catches the enum mismatch).
- ``module`` non-empty.
- ``extra="forbid"`` (round-trip with an extra field raises).

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.4
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.v2.descriptors.tool import ToolDescriptor
from app.v2.tool_tags import ToolCapabilityTag


def _baseline_kwargs(**overrides):
    base = dict(
        name="slack_post_message",
        description="post a message into a Slack channel",
        tags={
            ToolCapabilityTag.WRITE_EXTERNAL,
            ToolCapabilityTag.SEND_MESSAGE,
        },
        module="app.v2.adapters.slack",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_constructs_with_canonical_kwargs():
    d = ToolDescriptor(**_baseline_kwargs())
    assert d.name == "slack_post_message"
    assert ToolCapabilityTag.SEND_MESSAGE in d.tags


# ---------------------------------------------------------------------------
# name validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "slack_post_message",
        "x",
        "a1",
        "snake_case_with_digits_42",
    ],
)
def test_name_accepts_snake_case(name):
    ToolDescriptor(**_baseline_kwargs(name=name))


@pytest.mark.parametrize(
    "name",
    [
        "SlackPostMessage",   # CamelCase
        "slack-post-message", # kebab
        "1starts_with_digit",
        "trailing space ",
        "with.dot",
        "",
    ],
)
def test_name_rejects_non_snake_case(name):
    with pytest.raises(ValidationError):
        ToolDescriptor(**_baseline_kwargs(name=name))


# ---------------------------------------------------------------------------
# description
# ---------------------------------------------------------------------------


def test_description_min_length():
    with pytest.raises(ValidationError):
        ToolDescriptor(**_baseline_kwargs(description="short"))


def test_description_accepts_exact_min_length():
    """8 chars is the minimum; any longer is fine."""
    ToolDescriptor(**_baseline_kwargs(description="x" * 8))


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------


def test_tags_non_empty():
    with pytest.raises(ValidationError):
        ToolDescriptor(**_baseline_kwargs(tags=set()))


def test_tags_rejects_unknown_value():
    with pytest.raises(ValidationError):
        ToolDescriptor(**_baseline_kwargs(tags={"made_up_tag"}))


def test_tags_accepts_single_canonical_value():
    ToolDescriptor(
        **_baseline_kwargs(tags={ToolCapabilityTag.READ_EXTERNAL})
    )


def test_tags_drift_guard_every_value_constructible():
    """Every canonical tag must be representable in a
    ToolDescriptor. Catches an enum/descriptor mismatch."""
    for tag in ToolCapabilityTag:
        ToolDescriptor(**_baseline_kwargs(tags={tag}))


# ---------------------------------------------------------------------------
# module
# ---------------------------------------------------------------------------


def test_module_non_empty():
    with pytest.raises(ValidationError):
        ToolDescriptor(**_baseline_kwargs(module=""))


# ---------------------------------------------------------------------------
# extra-field rejection
# ---------------------------------------------------------------------------


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        ToolDescriptor(**_baseline_kwargs(category="messaging"))
