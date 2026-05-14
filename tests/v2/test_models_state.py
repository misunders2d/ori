"""Tests for ``app.v2.models.state``.

Pins:
- ``version`` defaults to 1; values <1 rejected.
- ``value`` accepts strings / ints / floats / bools / lists /
  dicts.
- ``written_by_run`` is optional (None for author-time seeds).
- extra fields rejected.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.7
- ``docs/PHASE_1_PLAN.md`` §4.8 / §5.1
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.v2.models.state import ScheduleState


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


def _baseline_kwargs(**overrides):
    base = dict(
        schedule_id="linux_mastery",
        key="last_fired_day",
        value=3,
        version=1,
        written_at=_NOW,
        written_by_run="r0000001-1111-1111-1111-111111111111",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Required fields + defaults
# ---------------------------------------------------------------------------


def test_version_defaults_to_one():
    kwargs = _baseline_kwargs()
    kwargs.pop("version")
    s = ScheduleState(**kwargs)
    assert s.version == 1


def test_version_rejects_zero():
    with pytest.raises(ValidationError):
        ScheduleState(**_baseline_kwargs(version=0))


def test_version_rejects_negative():
    with pytest.raises(ValidationError):
        ScheduleState(**_baseline_kwargs(version=-3))


def test_written_by_run_optional():
    """Author-time seeds (e.g. initial syllabus loaded at freeze)
    have no Run id to attribute to. None is valid."""
    s = ScheduleState(**_baseline_kwargs(written_by_run=None))
    assert s.written_by_run is None


# ---------------------------------------------------------------------------
# Value type coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "linux ls -la",
        42,
        3.14,
        True,
        False,
        None,
        ["a", "b", "c"],
        {"day": 5, "cmd": "ls -la"},
        [{"item": 1}, {"item": 2}],
        {"nested": {"deep": ["x", "y"]}},
    ],
)
def test_value_accepts_json_shapes(value):
    s = ScheduleState(**_baseline_kwargs(value=value))
    assert s.value == value


# ---------------------------------------------------------------------------
# extra-field rejection
# ---------------------------------------------------------------------------


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        ScheduleState(**_baseline_kwargs(checksum="abc"))
