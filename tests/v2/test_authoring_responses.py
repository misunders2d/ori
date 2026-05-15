"""Tests for ``app.v2.authoring.responses``.

Phase 7 slice 1 per ``docs/PHASE_7_PLAN.md`` §5.1.

Pins:
- Each factory (`ok` / `validation_failed` / `not_ready` /
  `cache_unavailable` / `not_found`) returns a
  :class:`ToolResponse` with the documented ``status``.
- Only the per-status payload fields are populated; others
  are ``None``.
- Round-trip via ``model_dump_json`` / ``model_validate_json``
  preserves every populated field.
- Unknown ``status`` value → :class:`ValidationError`.
- ``extra="forbid"`` rejects unknown keys.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.v2.authoring.responses import (
    ToolResponse,
    ToolResponseStatus,
)
from app.v2.validation import ValidationIssue


# ---------------------------------------------------------------------------
# Status enum surface
# ---------------------------------------------------------------------------


def test_status_literal_covers_five_values():
    import typing

    args = set(typing.get_args(ToolResponseStatus))
    assert args == {
        "ok",
        "validation_failed",
        "not_ready",
        "cache_unavailable",
        "not_found",
    }


def test_unknown_status_value_rejected():
    with pytest.raises(ValidationError):
        ToolResponse(status="nope")  # type: ignore[arg-type]


def test_extra_keys_forbidden():
    with pytest.raises(ValidationError):
        ToolResponse(status="ok", unknown_field="x")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Discriminated payload allowlist (round-1 reviewer slice-1 L48)
# ---------------------------------------------------------------------------


def test_ok_with_issues_rejected():
    """``status='ok'`` must not carry an ``issues`` list —
    that payload belongs to ``validation_failed``."""
    issues = [
        ValidationIssue(
            code="x", severity="error", path="p", message="m"
        )
    ]
    with pytest.raises(ValidationError, match="forbidden payload"):
        ToolResponse(status="ok", issues=issues)


def test_ok_with_missing_fields_rejected():
    with pytest.raises(ValidationError, match="forbidden payload"):
        ToolResponse(status="ok", missing_fields=["x"])


def test_ok_with_cache_kind_rejected():
    with pytest.raises(ValidationError, match="forbidden payload"):
        ToolResponse(status="ok", cache_kind="slack_channels")


def test_validation_failed_with_draft_id_rejected():
    issues = [
        ValidationIssue(
            code="x", severity="error", path="p", message="m"
        )
    ]
    with pytest.raises(ValidationError, match="forbidden payload"):
        ToolResponse(
            status="validation_failed",
            issues=issues,
            draft_id="d_1",
        )


def test_validation_failed_with_message_rejected():
    issues = [
        ValidationIssue(
            code="x", severity="error", path="p", message="m"
        )
    ]
    with pytest.raises(ValidationError, match="forbidden payload"):
        ToolResponse(
            status="validation_failed",
            issues=issues,
            message="x",
        )


def test_not_ready_with_issues_rejected():
    issues = [
        ValidationIssue(
            code="x", severity="error", path="p", message="m"
        )
    ]
    with pytest.raises(ValidationError, match="forbidden payload"):
        ToolResponse(
            status="not_ready",
            missing_fields=["x"],
            issues=issues,
        )


def test_not_ready_with_draft_id_rejected():
    with pytest.raises(ValidationError, match="forbidden payload"):
        ToolResponse(
            status="not_ready",
            missing_fields=["x"],
            draft_id="d_1",
        )


def test_cache_unavailable_with_draft_id_rejected():
    with pytest.raises(ValidationError, match="forbidden payload"):
        ToolResponse(
            status="cache_unavailable",
            cache_kind="slack_channels",
            network_error="x",
            draft_id="d_1",
        )


def test_cache_unavailable_with_message_rejected():
    with pytest.raises(ValidationError, match="forbidden payload"):
        ToolResponse(
            status="cache_unavailable",
            cache_kind="slack_channels",
            network_error="x",
            message="hint",
        )


def test_not_found_with_draft_id_rejected():
    with pytest.raises(ValidationError, match="forbidden payload"):
        ToolResponse(
            status="not_found",
            message="missing",
            draft_id="d_1",
        )


def test_not_found_with_issues_rejected():
    issues = [
        ValidationIssue(
            code="x", severity="error", path="p", message="m"
        )
    ]
    with pytest.raises(ValidationError, match="forbidden payload"):
        ToolResponse(
            status="not_found",
            message="x",
            issues=issues,
        )


def test_factory_helpers_pass_validator():
    """All five factory paths produce models the discriminator
    validator accepts — regression pin for the round-1 fix."""
    ToolResponse.ok(draft_id="d", spec={"k": "v"}, message="m")
    ToolResponse.validation_failed(
        issues=[
            ValidationIssue(
                code="x", severity="error", path="p", message="m"
            )
        ]
    )
    ToolResponse.not_ready(missing_fields=["trigger"])
    ToolResponse.cache_unavailable(
        kind="slack_channels", network_error="dns"
    )
    ToolResponse.not_found(message="missing")


# ---------------------------------------------------------------------------
# ok
# ---------------------------------------------------------------------------


def test_ok_factory_empty_payload():
    r = ToolResponse.ok()
    assert r.status == "ok"
    # All payload fields untouched.
    assert r.draft_id is None
    assert r.schedule_id is None
    assert r.spec is None
    assert r.issues is None
    assert r.missing_fields is None
    assert r.cache_kind is None
    assert r.network_error is None
    assert r.message is None


def test_ok_factory_draft_id():
    r = ToolResponse.ok(draft_id="draft_abc")
    assert r.status == "ok"
    assert r.draft_id == "draft_abc"
    assert r.schedule_id is None


def test_ok_factory_schedule_id():
    r = ToolResponse.ok(schedule_id="sched_x", message="paused")
    assert r.status == "ok"
    assert r.schedule_id == "sched_x"
    assert r.message == "paused"


def test_ok_factory_spec_dict():
    r = ToolResponse.ok(spec={"id": "abc", "hash": "h"})
    assert r.status == "ok"
    assert r.spec == {"id": "abc", "hash": "h"}


# ---------------------------------------------------------------------------
# validation_failed
# ---------------------------------------------------------------------------


def test_validation_failed_carries_issues():
    issues = [
        ValidationIssue(
            code="hash_required",
            severity="error",
            path="hash",
            message="required",
        )
    ]
    r = ToolResponse.validation_failed(issues=issues)
    assert r.status == "validation_failed"
    assert r.issues == issues
    # Other payloads untouched.
    assert r.draft_id is None
    assert r.missing_fields is None
    assert r.cache_kind is None


def test_validation_failed_empty_list_allowed():
    """Edge — caller could pass an empty list; the model
    doesn't enforce non-empty (issues semantics are the
    caller's contract)."""
    r = ToolResponse.validation_failed(issues=[])
    assert r.status == "validation_failed"
    assert r.issues == []


# ---------------------------------------------------------------------------
# not_ready
# ---------------------------------------------------------------------------


def test_not_ready_carries_missing_fields():
    r = ToolResponse.not_ready(
        missing_fields=["trigger", "delivery"]
    )
    assert r.status == "not_ready"
    assert r.missing_fields == ["trigger", "delivery"]
    assert r.issues is None
    assert r.draft_id is None


# ---------------------------------------------------------------------------
# cache_unavailable
# ---------------------------------------------------------------------------


def test_cache_unavailable_carries_kind_and_error():
    r = ToolResponse.cache_unavailable(
        kind="slack_channels",
        network_error="DNS down",
    )
    assert r.status == "cache_unavailable"
    assert r.cache_kind == "slack_channels"
    assert r.network_error == "DNS down"
    assert r.draft_id is None
    assert r.issues is None


# ---------------------------------------------------------------------------
# not_found
# ---------------------------------------------------------------------------


def test_not_found_carries_message():
    r = ToolResponse.not_found(
        message="draft draft_abc missing"
    )
    assert r.status == "not_found"
    assert r.message == "draft draft_abc missing"
    assert r.draft_id is None


# ---------------------------------------------------------------------------
# Round-trip preservation
# ---------------------------------------------------------------------------


def test_round_trip_ok_with_spec():
    r = ToolResponse.ok(
        draft_id="d_1",
        spec={"id": "abc", "hash": "h", "n": 7},
        message="compiled",
    )
    rebuilt = ToolResponse.model_validate_json(r.model_dump_json())
    assert rebuilt == r


def test_round_trip_validation_failed():
    issues = [
        ValidationIssue(
            code="hash_required",
            severity="error",
            path="hash",
            message="m",
        ),
        ValidationIssue(
            code="other",
            severity="warning",
            path="x",
            message="y",
        ),
    ]
    r = ToolResponse.validation_failed(issues=issues)
    rebuilt = ToolResponse.model_validate_json(r.model_dump_json())
    assert rebuilt == r


def test_round_trip_cache_unavailable():
    r = ToolResponse.cache_unavailable(
        kind="slack_channels",
        network_error="403 forbidden",
    )
    rebuilt = ToolResponse.model_validate_json(r.model_dump_json())
    assert rebuilt == r


def test_round_trip_not_ready():
    r = ToolResponse.not_ready(
        missing_fields=["owner", "trigger"]
    )
    rebuilt = ToolResponse.model_validate_json(r.model_dump_json())
    assert rebuilt == r


def test_round_trip_not_found():
    r = ToolResponse.not_found(message="schedule sched_X missing")
    rebuilt = ToolResponse.model_validate_json(r.model_dump_json())
    assert rebuilt == r
