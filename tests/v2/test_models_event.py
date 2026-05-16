"""Tests for ``app.v2.models.event``.

Pins:
- ``kind`` is constrained to the canonical EventKind enum.
- Every canonical EventKind value round-trips (model → json →
  model).
- ``run_id`` and ``correlates`` are optional.
- ``payload`` accepts any JSON-serialisable dict (phase 1).
- extra fields rejected.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0 (canonical EventKind list)
- ``docs/PHASE_1_PLAN.md`` §4.7 / §5.1
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from app.v2.enums import EventKind
from app.v2.models.event import Event, RunSucceededPayload


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


def _baseline_kwargs(**overrides):
    base = dict(
        id="e0000001-1111-1111-1111-111111111111",
        run_id="r0000001-1111-1111-1111-111111111111",
        schedule_id="daily_audit",
        ts=_NOW,
        kind=EventKind.RUN_STARTED,
        payload={"actor": "worker_a"},
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Required fields + defaults
# ---------------------------------------------------------------------------


def test_baseline_event_valid():
    e = Event(**_baseline_kwargs())
    assert e.kind == EventKind.RUN_STARTED
    assert e.run_id is not None
    assert e.correlates is None
    assert e.payload == {"actor": "worker_a"}


def test_run_id_optional():
    """Schedule-level events (``schedule_created``,
    ``schedule_archived``) have no Run yet, so ``run_id`` is
    None."""
    e = Event(
        **_baseline_kwargs(
            run_id=None,
            kind=EventKind.SCHEDULE_CREATED,
        )
    )
    assert e.run_id is None


def test_payload_defaults_empty_dict():
    kwargs = _baseline_kwargs()
    kwargs.pop("payload")
    e = Event(**kwargs)
    assert e.payload == {}


def test_correlates_optional():
    e = Event(**_baseline_kwargs(correlates="some-other-event-id"))
    assert e.correlates == "some-other-event-id"


def test_payload_accepts_nested_structures():
    payload = {
        "duration_ms": 1234,
        "emit_results": [
            {"adapter": "slack_post", "channel": "C012", "ok": True},
            {"adapter": "sheet_append", "rows_written": 1},
        ],
        "render_state_keys": ["sales_30d", "step_1"],
    }
    e = Event(**_baseline_kwargs(payload=payload))
    assert e.payload == payload


# ---------------------------------------------------------------------------
# EventKind enum coverage — round-trip every canonical value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(EventKind))
def test_every_event_kind_round_trips(kind):
    """Every value in the canonical EventKind enum must round-trip
    through model construction + JSON serialisation. This is the
    test the design contract §4.0 expects (`every event kind in
    the canonical list has at least one round-trip test`)."""
    e = Event(**_baseline_kwargs(kind=kind))
    payload = e.model_dump(mode="json")
    assert payload["kind"] == kind.value
    restored = Event(**payload)
    assert restored.kind == kind


def test_kind_rejects_unknown_string():
    with pytest.raises(ValidationError):
        Event(**_baseline_kwargs(kind="ran_a_little_late"))


# ---------------------------------------------------------------------------
# RunSucceededPayload — phase-11 slice-6 typed discriminator (Option B)
# ---------------------------------------------------------------------------


def test_run_succeeded_payload_additive_default_false():
    """skipped_unchanged is ADDITIVE + DEFAULTED: a
    pre-slice-6 producer that only sets worker_id gets
    False — semantically unaffected (slice-4 discipline)."""
    p = RunSucceededPayload(worker_id="w1")
    assert p.skipped_unchanged is False
    assert p.model_dump() == {
        "worker_id": "w1",
        "skipped_unchanged": False,
    }


def test_run_succeeded_payload_skip_sets_true():
    p = RunSucceededPayload(worker_id="w1", skipped_unchanged=True)
    assert p.model_dump()["skipped_unchanged"] is True


def test_run_succeeded_payload_extra_forbidden():
    with pytest.raises(ValidationError):
        RunSucceededPayload(worker_id="w1", bogus=1)


def test_run_succeeded_distinguishability_via_typed_field():
    """A consumer DETERMINISTICALLY separates
    succeeded-by-delivering from succeeded-by-skip via the
    TYPED field — not a heuristic / ad-hoc dict probe."""
    delivered = Event(
        **_baseline_kwargs(kind=EventKind.RUN_SUCCEEDED)
    ).model_copy(
        update={
            "payload": RunSucceededPayload(
                worker_id="w1"
            ).model_dump()
        }
    )
    skipped = delivered.model_copy(
        update={
            "payload": RunSucceededPayload(
                worker_id="w1", skipped_unchanged=True
            ).model_dump()
        }
    )

    def was_skip(ev: Event) -> bool:
        return RunSucceededPayload(**ev.payload).skipped_unchanged

    assert was_skip(delivered) is False
    assert was_skip(skipped) is True


# ---------------------------------------------------------------------------
# JSON round-trip via TypeAdapter
# ---------------------------------------------------------------------------


def test_event_json_round_trip():
    """Full round-trip through a JSON string. Used as a fixture
    for storage-layer tests later in phase 1."""
    import json

    adapter = TypeAdapter(Event)
    e = Event(**_baseline_kwargs())
    json_str = adapter.dump_json(e).decode()
    restored = adapter.validate_json(json_str)
    assert restored.kind == e.kind
    assert restored.schedule_id == e.schedule_id
    assert restored.run_id == e.run_id

    # Equivalence via dump_python(mode="json").
    dumped = adapter.dump_python(e, mode="json")
    re_parsed = adapter.validate_python(json.loads(json.dumps(dumped)))
    assert re_parsed.kind == e.kind


# ---------------------------------------------------------------------------
# extra-field rejection
# ---------------------------------------------------------------------------


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        Event(**_baseline_kwargs(severity="critical"))
