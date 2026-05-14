"""Tests for ``app.v2.descriptors.emit``.

Pins per ``docs/PHASE_2_PLAN.md`` §5.4:

**EmitDescriptor:**
- Requires ``SEND_MESSAGE`` or ``WRITE_EXTERNAL`` in tags.
- ``target_kind`` non-empty + snake_case.
- ``supports_native_dedup`` boolean.
- ``id`` snake_case.

**EmitInputContract:**
- ``idempotency_key`` MUST equal compute_idempotency_key
  applied to the same triple (round-7 invariant — runtime never
  passes a hand-rolled key).
- ``payload`` accepts dict or str.

**EmitOutputContract:**
- ``delivered=True`` requires ``error is None``.
- ``delivered=False`` requires non-empty ``error``.
- ``attempted_at`` is timezone-aware (UTC).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.v2.descriptors.emit import (
    EmitDescriptor,
    EmitInputContract,
    EmitOutputContract,
)
from app.v2.idempotency import compute_idempotency_key
from app.v2.tool_tags import ToolCapabilityTag


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


# ===========================================================================
# EmitDescriptor
# ===========================================================================


def _descriptor_kwargs(**overrides):
    base = dict(
        id="slack_post_message",
        description="post a message into a Slack channel",
        tags={
            ToolCapabilityTag.WRITE_EXTERNAL,
            ToolCapabilityTag.SEND_MESSAGE,
        },
        target_kind="slack",
        supports_native_dedup=True,
    )
    base.update(overrides)
    return base


def test_descriptor_constructs_with_canonical_kwargs():
    d = EmitDescriptor(**_descriptor_kwargs())
    assert d.id == "slack_post_message"
    assert d.target_kind == "slack"
    assert d.supports_native_dedup is True


def test_descriptor_accepts_send_message_only():
    """A pure user-facing emit (Telegram DM) need not carry
    ``write_external`` — ``send_message`` alone is enough."""
    EmitDescriptor(
        **_descriptor_kwargs(
            tags={ToolCapabilityTag.SEND_MESSAGE},
        )
    )


def test_descriptor_accepts_write_external_only():
    """A non-user-facing write (Drive append, Sheet write) carries
    ``write_external`` without ``send_message``."""
    EmitDescriptor(
        **_descriptor_kwargs(
            id="drive_append",
            description="append a row to a Drive file",
            tags={ToolCapabilityTag.WRITE_EXTERNAL},
            target_kind="drive",
            supports_native_dedup=False,
        )
    )


def test_descriptor_rejects_when_neither_write_tag_present():
    with pytest.raises(ValidationError, match="SEND_MESSAGE or WRITE_EXTERNAL"):
        EmitDescriptor(
            **_descriptor_kwargs(
                tags={ToolCapabilityTag.READ_EXTERNAL},
            )
        )


def test_descriptor_id_must_be_snake_case():
    with pytest.raises(ValidationError, match="snake_case"):
        EmitDescriptor(**_descriptor_kwargs(id="SlackPostMessage"))


def test_descriptor_target_kind_must_be_snake_case():
    with pytest.raises(ValidationError, match="snake_case"):
        EmitDescriptor(**_descriptor_kwargs(target_kind="Slack"))


def test_descriptor_target_kind_non_empty():
    with pytest.raises(ValidationError):
        EmitDescriptor(**_descriptor_kwargs(target_kind=""))


def test_descriptor_extra_field_rejected():
    with pytest.raises(ValidationError):
        EmitDescriptor(**_descriptor_kwargs(rate_limit=5))


# ===========================================================================
# EmitInputContract
# ===========================================================================


def _input_kwargs(**overrides):
    sched = "daily_audit"
    root = "root-abc"
    emit = "post_slack"
    base = dict(
        schedule_id=sched,
        root_run_id=root,
        emit_id=emit,
        idempotency_key=compute_idempotency_key(
            schedule_id=sched, root_run_id=root, emit_id=emit
        ),
        payload={"text": "hello"},
    )
    base.update(overrides)
    return base


def test_input_constructs_with_canonical_kwargs():
    EmitInputContract(**_input_kwargs())


def test_input_accepts_string_payload():
    EmitInputContract(**_input_kwargs(payload="pre-rendered text"))


def test_input_idempotency_key_must_match_components():
    """Round-7 invariant: the runtime always derives the key
    from (schedule_id, root_run_id, emit_id). A descriptor
    accepting a hand-rolled key would silently break dedup."""
    with pytest.raises(ValidationError, match="idempotency_key"):
        EmitInputContract(
            **_input_kwargs(idempotency_key="hand-rolled-bogus")
        )


def test_input_idempotency_key_stable_across_retries():
    """Two inputs for the same (schedule, root_run, emit)
    produce identical keys regardless of any out-of-band data
    (attempt, wall-clock). The key is the dedup anchor."""
    a = EmitInputContract(**_input_kwargs())
    b = EmitInputContract(**_input_kwargs(payload="different content"))
    assert a.idempotency_key == b.idempotency_key


def test_input_idempotency_key_differs_per_emit():
    a = EmitInputContract(**_input_kwargs(emit_id="post_slack",
                                          idempotency_key=compute_idempotency_key(
                                              schedule_id="daily_audit",
                                              root_run_id="root-abc",
                                              emit_id="post_slack",
                                          )))
    b = EmitInputContract(**_input_kwargs(emit_id="log_sheet",
                                          idempotency_key=compute_idempotency_key(
                                              schedule_id="daily_audit",
                                              root_run_id="root-abc",
                                              emit_id="log_sheet",
                                          )))
    assert a.idempotency_key != b.idempotency_key


@pytest.mark.parametrize(
    "field",
    ["schedule_id", "root_run_id", "emit_id", "idempotency_key"],
)
def test_input_required_string_fields_min_length(field):
    with pytest.raises(ValidationError):
        EmitInputContract(**_input_kwargs(**{field: ""}))


def test_input_extra_field_rejected():
    with pytest.raises(ValidationError):
        EmitInputContract(**_input_kwargs(extra_field="x"))


# ===========================================================================
# EmitOutputContract
# ===========================================================================


def test_output_delivered_true_with_no_error():
    o = EmitOutputContract(
        delivered=True,
        destination_id="1234.5678",
        attempted_at=_NOW,
        error=None,
    )
    assert o.delivered is True
    assert o.error is None


def test_output_delivered_true_forbids_error():
    with pytest.raises(ValidationError, match="forbids a non-null error"):
        EmitOutputContract(
            delivered=True,
            destination_id="1234.5678",
            attempted_at=_NOW,
            error="something failed",
        )


def test_output_delivered_false_requires_error():
    """A failed delivery must carry an error message so the
    failure monitor + admin alert have signal to act on."""
    with pytest.raises(
        ValidationError, match="requires a non-empty error"
    ):
        EmitOutputContract(
            delivered=False,
            destination_id=None,
            attempted_at=_NOW,
            error=None,
        )


def test_output_delivered_false_rejects_empty_error_string():
    with pytest.raises(
        ValidationError, match="requires a non-empty error"
    ):
        EmitOutputContract(
            delivered=False,
            destination_id=None,
            attempted_at=_NOW,
            error="",
        )


def test_output_delivered_false_with_error_ok():
    o = EmitOutputContract(
        delivered=False,
        destination_id=None,
        attempted_at=_NOW,
        error="slack 503",
    )
    assert o.delivered is False
    assert o.error == "slack 503"


def test_output_destination_id_optional():
    """Some emit adapters have nothing to return as a
    destination id (Drive append returns a row id; an arbitrary
    file write may have nothing similar)."""
    EmitOutputContract(
        delivered=True,
        destination_id=None,
        attempted_at=_NOW,
        error=None,
    )


def test_output_attempted_at_must_be_timezone_aware():
    naive = datetime(2026, 5, 15, 9, 0)
    with pytest.raises(ValidationError, match="timezone-aware"):
        EmitOutputContract(
            delivered=True,
            destination_id=None,
            attempted_at=naive,
            error=None,
        )


def test_output_attempted_at_must_be_utc():
    eastern = datetime(2026, 5, 15, 9, 0, tzinfo=timezone(timedelta(hours=-5)))
    with pytest.raises(ValidationError, match="UTC"):
        EmitOutputContract(
            delivered=True,
            destination_id=None,
            attempted_at=eastern,
            error=None,
        )


def test_output_extra_field_rejected():
    with pytest.raises(ValidationError):
        EmitOutputContract(
            delivered=True,
            destination_id=None,
            attempted_at=_NOW,
            error=None,
            latency_ms=42,
        )
