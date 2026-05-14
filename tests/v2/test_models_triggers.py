"""Tests for ``app.v2.models.triggers``.

Pins:
- All five Trigger subclasses exist and discriminate correctly
  via the ``type`` field.
- Cron / OneOff validators reject the most common author bugs
  (wrong field count, empty timezone, etc.).
- Interval / Event / Conditional are shape-stubs only — they
  accept any valid Pydantic input without extra checks. Their
  validators land in phase 3.
- The ``Trigger`` discriminated union deserializes from JSON
  deterministically based on the ``type`` field.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.2
- ``docs/PHASE_1_PLAN.md`` §5.1 (test_models_triggers.py)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from app.v2.models.triggers import (
    ConditionalTrigger,
    CronTrigger,
    EventTrigger,
    IntervalTrigger,
    OneOffTrigger,
    Trigger,
)


# ---------------------------------------------------------------------------
# CronTrigger
# ---------------------------------------------------------------------------


def test_cron_trigger_valid_basic():
    t = CronTrigger(cron="0 18 * * MON-FRI", timezone="Europe/Kyiv")
    assert t.type == "cron"
    assert t.cron == "0 18 * * MON-FRI"
    assert t.timezone == "Europe/Kyiv"


def test_cron_trigger_default_timezone_utc():
    t = CronTrigger(cron="0 9 * * *")
    assert t.timezone == "UTC"


def test_cron_trigger_rejects_wrong_field_count():
    """Phase-1 syntactic check: cron must have exactly 5
    whitespace-separated fields. Deeper validation (valid ranges,
    DOW names, step expressions) lives at APScheduler later."""
    with pytest.raises(ValidationError, match="5 whitespace-separated"):
        CronTrigger(cron="0 18 * *")  # 4 fields

    with pytest.raises(ValidationError, match="5 whitespace-separated"):
        CronTrigger(cron="0 18 * * MON every 5 minutes")  # 7 fields


def test_cron_trigger_rejects_empty_timezone():
    with pytest.raises(ValidationError, match="timezone must be non-empty"):
        CronTrigger(cron="0 9 * * *", timezone="")


def test_cron_trigger_strips_surrounding_whitespace():
    """Trailing / leading whitespace on the cron expression is
    forgiven (counts after trimming). Confirms the trim happens
    before the field-count check."""
    t = CronTrigger(cron="  0 18 * * MON  ")
    assert t.cron == "0 18 * * MON"


def test_cron_trigger_type_literal_only():
    """The discriminator field must literally be 'cron'."""
    with pytest.raises(ValidationError):
        CronTrigger(type="hourly", cron="0 * * * *")


def test_cron_trigger_forbids_extra_fields():
    with pytest.raises(ValidationError):
        CronTrigger(cron="0 9 * * *", timezone="UTC", retries=3)


# ---------------------------------------------------------------------------
# OneOffTrigger
# ---------------------------------------------------------------------------


def test_one_off_trigger_valid():
    t = OneOffTrigger(
        at_iso_datetime="2026-05-15T14:00:00+03:00",
        timezone="Europe/Kyiv",
    )
    assert t.type == "one_off"
    assert isinstance(t.at_iso_datetime, datetime)


def test_one_off_trigger_accepts_datetime_object():
    when = datetime(2026, 5, 15, 14, 0, tzinfo=timezone.utc)
    t = OneOffTrigger(at_iso_datetime=when, timezone="UTC")
    assert t.at_iso_datetime == when


def test_one_off_trigger_rejects_garbled_datetime():
    with pytest.raises(ValidationError):
        OneOffTrigger(at_iso_datetime="next tuesday", timezone="UTC")


def test_one_off_trigger_rejects_empty_timezone():
    with pytest.raises(ValidationError, match="timezone must be non-empty"):
        OneOffTrigger(at_iso_datetime="2026-05-15T14:00:00Z", timezone="   ")


def test_one_off_trigger_default_timezone_utc():
    t = OneOffTrigger(at_iso_datetime="2026-05-15T14:00:00Z")
    assert t.timezone == "UTC"


def test_one_off_trigger_forbids_extra_fields():
    with pytest.raises(ValidationError):
        OneOffTrigger(
            at_iso_datetime="2026-05-15T14:00:00Z",
            timezone="UTC",
            recurring=False,
        )


# ---------------------------------------------------------------------------
# IntervalTrigger (phase-3 stub)
# ---------------------------------------------------------------------------


def test_interval_trigger_valid():
    t = IntervalTrigger(every_seconds=300)
    assert t.type == "interval"
    assert t.every_seconds == 300


def test_interval_trigger_rejects_non_positive():
    with pytest.raises(ValidationError):
        IntervalTrigger(every_seconds=0)
    with pytest.raises(ValidationError):
        IntervalTrigger(every_seconds=-1)


# ---------------------------------------------------------------------------
# EventTrigger (phase-3 stub)
# ---------------------------------------------------------------------------


def test_event_trigger_valid():
    t = EventTrigger(event="new_email_received")
    assert t.type == "event"
    assert t.event == "new_email_received"


def test_event_trigger_forbids_extra_fields():
    with pytest.raises(ValidationError):
        EventTrigger(event="x", filter="...")


# ---------------------------------------------------------------------------
# ConditionalTrigger (phase-3 stub)
# ---------------------------------------------------------------------------


def test_conditional_trigger_valid():
    t = ConditionalTrigger(gate="inventory_below_threshold", poll_seconds=600)
    assert t.type == "conditional"


def test_conditional_trigger_rejects_zero_poll():
    with pytest.raises(ValidationError):
        ConditionalTrigger(gate="g", poll_seconds=0)


# ---------------------------------------------------------------------------
# Discriminator union — Trigger
# ---------------------------------------------------------------------------


def _adapter():
    """Pydantic v2 TypeAdapter for the Trigger annotated union."""
    return TypeAdapter(Trigger)


def test_trigger_union_picks_cron_by_type():
    adapter = _adapter()
    t = adapter.validate_python(
        {"type": "cron", "cron": "0 9 * * *", "timezone": "UTC"}
    )
    assert isinstance(t, CronTrigger)


def test_trigger_union_picks_one_off_by_type():
    adapter = _adapter()
    t = adapter.validate_python(
        {
            "type": "one_off",
            "at_iso_datetime": "2026-05-15T14:00:00Z",
            "timezone": "UTC",
        }
    )
    assert isinstance(t, OneOffTrigger)


def test_trigger_union_picks_interval_by_type():
    adapter = _adapter()
    t = adapter.validate_python({"type": "interval", "every_seconds": 60})
    assert isinstance(t, IntervalTrigger)


def test_trigger_union_picks_event_by_type():
    adapter = _adapter()
    t = adapter.validate_python({"type": "event", "event": "x"})
    assert isinstance(t, EventTrigger)


def test_trigger_union_picks_conditional_by_type():
    adapter = _adapter()
    t = adapter.validate_python(
        {"type": "conditional", "gate": "g", "poll_seconds": 30}
    )
    assert isinstance(t, ConditionalTrigger)


def test_trigger_union_rejects_unknown_type():
    """Unknown discriminator value rejects with a clear error
    (no fallback / no silent first-match)."""
    adapter = _adapter()
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "every_full_moon"})


def test_trigger_union_rejects_missing_type():
    adapter = _adapter()
    with pytest.raises(ValidationError):
        adapter.validate_python({"cron": "0 9 * * *"})


def test_trigger_union_roundtrips_through_json():
    """JSON → model → JSON should preserve shape exactly. Used
    as a fixture for storage-layer tests in later phase-1
    commits."""
    import json

    adapter = _adapter()
    payload = {
        "type": "cron",
        "cron": "0 18 * * MON",
        "timezone": "Europe/Kyiv",
    }
    model = adapter.validate_python(payload)
    serialized = adapter.dump_python(model, mode="json")
    assert serialized == payload
    # Round-trip through JSON string too.
    re_parsed = adapter.validate_json(json.dumps(serialized))
    assert isinstance(re_parsed, CronTrigger)
    assert re_parsed.cron == "0 18 * * MON"
