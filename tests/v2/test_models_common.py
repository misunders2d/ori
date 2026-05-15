"""Tests for ``app.v2.models.common``.

Phase 9 slice 1 per ``docs/PHASE_9_PLAN.md`` §5.8.

Pins for the phase-9 amendment to ``TemplateRef``:

- ``TemplateRef.args`` defaults to ``None``.
- ``args=None`` round-trips through JSON serialise +
  deserialise.
- Populated ``args`` (incl. nested JSON shapes)
  round-trips.
- Non-JSON values (datetime, set, custom class) raise
  ``pydantic.ValidationError``.
- ``args`` field accepts the documented JSON-primitive
  shapes (bool / int / float / str / None / list /
  nested dict).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.v2.models.common import TemplateRef


# ===========================================================================
# Defaults
# ===========================================================================


def test_args_defaults_to_none():
    ref = TemplateRef(name="OneOffReminder", version="1")
    assert ref.args is None


def test_args_explicit_none():
    ref = TemplateRef(name="OneOffReminder", version="1", args=None)
    assert ref.args is None


def test_args_empty_dict_accepted():
    ref = TemplateRef(name="OneOffReminder", version="1", args={})
    assert ref.args == {}


# ===========================================================================
# Round-trip
# ===========================================================================


def test_round_trip_none_args():
    original = TemplateRef(name="OneOffReminder", version="1")
    blob = original.model_dump_json()
    loaded = TemplateRef.model_validate_json(blob)
    assert loaded == original


def test_round_trip_populated_text_args():
    original = TemplateRef(
        name="OneOffReminder",
        version="1",
        args={"text": "weekly digest reminder"},
    )
    blob = original.model_dump_json()
    loaded = TemplateRef.model_validate_json(blob)
    assert loaded == original
    assert loaded.args == {"text": "weekly digest reminder"}


def test_round_trip_nested_json_args():
    """Nested JSON shapes (list of dicts, nested dict) round-
    trip per the JsonValue recursive type."""
    original = TemplateRef(
        name="ChannelDigest",
        version="2",
        args={
            "channels": ["C1", "C2", "C3"],
            "metadata": {"author": "Sergey", "priority": 1},
            "snoozable": False,
            "max_age_hours": 24,
        },
    )
    blob = original.model_dump_json()
    loaded = TemplateRef.model_validate_json(blob)
    assert loaded == original


def test_round_trip_primitive_types():
    """JsonValue accepts each JSON primitive: bool / int /
    float / str / None."""
    original = TemplateRef(
        name="PrimitiveSampler",
        version="1",
        args={
            "is_active": True,
            "is_disabled": False,
            "count": 42,
            "ratio": 3.14,
            "label": "hello",
            "stash": None,
        },
    )
    blob = original.model_dump_json()
    loaded = TemplateRef.model_validate_json(blob)
    assert loaded == original


# ===========================================================================
# JsonValue enforcement (round-2 reviewer L48 fix)
# ===========================================================================


def test_args_rejects_datetime_value():
    """datetime is NOT a JSON primitive; the JsonValue
    type rejects it at validation time."""
    with pytest.raises(ValidationError):
        TemplateRef(
            name="OneOffReminder",
            version="1",
            args={"when": datetime(2026, 5, 15, tzinfo=timezone.utc)},
        )


def test_args_rejects_set_value():
    with pytest.raises(ValidationError):
        TemplateRef(
            name="OneOffReminder",
            version="1",
            args={"items": {1, 2, 3}},
        )


def test_args_rejects_bytes_value():
    with pytest.raises(ValidationError):
        TemplateRef(
            name="OneOffReminder",
            version="1",
            args={"blob": b"raw bytes"},
        )


def test_args_rejects_custom_class_value():
    class Marker:
        pass

    with pytest.raises(ValidationError):
        TemplateRef(
            name="OneOffReminder",
            version="1",
            args={"obj": Marker()},
        )


def test_args_rejects_nested_non_json_value():
    """JsonValue is recursive — non-JSON values nested
    inside a list / dict are still rejected."""
    with pytest.raises(ValidationError):
        TemplateRef(
            name="OneOffReminder",
            version="1",
            args={
                "outer": [
                    {"when": datetime(2026, 5, 15, tzinfo=timezone.utc)}
                ]
            },
        )


# ===========================================================================
# extra=forbid carry-forward
# ===========================================================================


def test_template_ref_rejects_extra_fields():
    with pytest.raises(ValidationError):
        TemplateRef(
            name="OneOffReminder",
            version="1",
            args={"text": "hi"},
            stray_field="nope",  # type: ignore[call-arg]
        )
