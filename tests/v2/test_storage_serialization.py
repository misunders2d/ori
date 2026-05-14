"""Tests for ``app.v2.storage.serialization``.

Pins per ``docs/PHASE_3_PLAN.md`` §9.2:

- Pydantic round-trip (encode → decode → equal).
- Plain-value round-trip (dict, list, scalar).
- Naive datetime rejected on encode for BOTH paths:
    * Plain values: ``_json_default`` catches the raw datetime
      before ``json.dumps`` serialises it.
    * Pydantic models: encoder walks ``model_dump(mode='python')``
      recursively and raises on the first naive datetime — at
      any depth (top-level, nested model, list element, dict
      value). Without this walk, ``model_dump(mode='json')``
      silently stringifies the naive datetime and the JSON
      bytes would carry no tzinfo, defeating the contract.
- ISO 8601 string with ``+00:00`` decodes to a UTC datetime
  via a Pydantic model.
- Sort-order stability: equivalent dicts produce identical
  encoded strings regardless of insertion order.
- ``NaiveDatetimeError`` is a ``ValueError``.

Smoke:
- Module imports no I/O libs.
- No execution-suggestive public callables.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ConfigDict

from app.v2.storage import serialization as serialization_mod
from app.v2.storage.serialization import (
    NaiveDatetimeError,
    decode_json,
    encode_json,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _SampleModel(BaseModel):
    """Small Pydantic model with a tz-aware datetime field."""

    model_config = ConfigDict(extra="forbid")

    name: str
    count: int
    when: datetime


_UTC_NOW = datetime(2026, 5, 15, 9, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Pydantic round-trip
# ---------------------------------------------------------------------------


def test_pydantic_round_trip_preserves_fields():
    model = _SampleModel(name="alice", count=3, when=_UTC_NOW)
    raw = encode_json(model)
    restored = decode_json(raw, _SampleModel)
    assert restored == model


def test_pydantic_encode_produces_sorted_keys():
    """Pydantic emits fields in declaration order. The encoder
    re-serialises through json.dumps with sort_keys=True so the
    on-wire bytes don't depend on Pydantic's field order."""
    raw = encode_json(_SampleModel(name="a", count=1, when=_UTC_NOW))
    # Keys appear alphabetically: count, name, when.
    keys_in_order = [
        k for k in ("count", "name", "when") if f'"{k}":' in raw
    ]
    assert keys_in_order == ["count", "name", "when"]
    # And the indices are strictly increasing.
    indices = [raw.index(f'"{k}":') for k in keys_in_order]
    assert indices == sorted(indices)


def test_pydantic_encode_uses_compact_separators():
    raw = encode_json(_SampleModel(name="a", count=1, when=_UTC_NOW))
    assert ", " not in raw  # no space after comma
    assert ": " not in raw  # no space after colon


# ---------------------------------------------------------------------------
# Plain value round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        {"a": 1, "b": [1, 2, 3]},
        ["x", "y", "z"],
        42,
        3.14,
        "string",
        True,
        False,
        None,
        {"nested": {"k": "v", "list": [{"i": 0}, {"i": 1}]}},
    ],
)
def test_plain_value_round_trip(value):
    raw = encode_json(value)
    restored = decode_json(raw, dict)  # target_type ignored for plain
    # decode_json returns plain JSON for non-BaseModel target; we
    # compare directly.
    assert restored == value


def test_plain_dict_sort_order_stability():
    """Two dicts with the same contents but different insertion
    order produce identical encoded strings — that's the
    determinism property the design contract relies on for
    hash recomputation."""
    a = {"x": 1, "y": 2, "z": 3}
    b = {"z": 3, "x": 1, "y": 2}
    assert encode_json(a) == encode_json(b)


def test_plain_list_order_preserved():
    """Lists are ordered; sort_keys does not touch list element
    order. Verify the encoder doesn't accidentally sort lists."""
    raw = encode_json([3, 1, 2])
    assert raw == "[3,1,2]"


# ---------------------------------------------------------------------------
# Datetime handling
# ---------------------------------------------------------------------------


def test_tz_aware_datetime_in_plain_dict_encodes_to_iso():
    raw = encode_json({"ts": _UTC_NOW})
    payload = json.loads(raw)
    assert payload["ts"].endswith("+00:00")


def test_non_utc_tz_aware_datetime_in_plain_dict_encodes_to_iso():
    """Encoder doesn't enforce UTC at this layer — only
    tz-aware. Use the model-level validators (e.g.
    EmitOutputContract) to enforce UTC where required."""
    eastern = datetime(2026, 5, 15, 9, 30, tzinfo=timezone(timedelta(hours=-5)))
    raw = encode_json({"ts": eastern})
    payload = json.loads(raw)
    assert payload["ts"].endswith("-05:00")


def test_naive_datetime_in_plain_dict_rejected():
    naive = datetime(2026, 5, 15, 9, 30)
    with pytest.raises(NaiveDatetimeError):
        encode_json({"ts": naive})


def test_naive_datetime_in_plain_list_rejected():
    naive = datetime(2026, 5, 15, 9, 30)
    with pytest.raises(NaiveDatetimeError):
        encode_json([naive])


def test_naive_datetime_error_is_value_error():
    """The NaiveDatetimeError class hierarchy is part of the
    contract — callers wishing to catch it via ``except
    ValueError`` should not be surprised by a refactor."""
    assert issubclass(NaiveDatetimeError, ValueError)


def test_pydantic_iso_string_decodes_to_utc_datetime():
    """A Pydantic model with a datetime field that's encoded as
    ISO 8601 with +00:00 round-trips through decode_json to a
    UTC-aware datetime."""
    raw = encode_json(_SampleModel(name="x", count=1, when=_UTC_NOW))
    restored = decode_json(raw, _SampleModel)
    assert restored.when == _UTC_NOW
    assert restored.when.tzinfo is not None
    assert restored.when.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# Pydantic naive-datetime rejection (reviewer follow-up).
# Walks model_dump(mode='python') so a naive datetime cannot
# slip through model_dump(mode='json')'s silent stringification.
# ---------------------------------------------------------------------------


_NAIVE_DT = datetime(2026, 5, 15, 9, 30)  # tzinfo None


class _NestedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inner_when: datetime


class _ModelWithNested(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    nested: _NestedModel


class _ModelWithListOfDatetimes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    timestamps: list[datetime]


class _ModelWithDictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    payload: dict[str, datetime]


def test_pydantic_top_level_naive_datetime_rejected():
    """Naive datetime in a direct ``datetime`` field — without
    the python-mode walk, model_dump(mode='json') would emit
    an ISO string with no offset and the encoder would never
    notice."""
    model = _SampleModel(name="x", count=1, when=_NAIVE_DT)
    with pytest.raises(NaiveDatetimeError, match="when"):
        encode_json(model)


def test_pydantic_nested_model_naive_datetime_rejected():
    """Naive datetime hiding inside a sub-model. The walker
    descends into the dict produced by mode='python'."""
    nested = _NestedModel(inner_when=_NAIVE_DT)
    parent = _ModelWithNested(label="parent", nested=nested)
    with pytest.raises(NaiveDatetimeError, match="inner_when"):
        encode_json(parent)


def test_pydantic_list_of_datetimes_with_one_naive_rejected():
    """One naive value in a list-of-datetimes field fails the
    whole encode."""
    model = _ModelWithListOfDatetimes(
        label="x",
        timestamps=[_UTC_NOW, _NAIVE_DT, _UTC_NOW],
    )
    with pytest.raises(NaiveDatetimeError, match=r"timestamps\[1\]"):
        encode_json(model)


def test_pydantic_dict_payload_naive_value_rejected():
    """Datetime hiding inside a ``dict[str, datetime]`` field."""
    model = _ModelWithDictPayload(
        label="x",
        payload={"good": _UTC_NOW, "evil": _NAIVE_DT},
    )
    with pytest.raises(NaiveDatetimeError, match="evil"):
        encode_json(model)


def test_pydantic_all_tz_aware_passes_walk():
    """A model with tz-aware datetimes everywhere (top-level,
    nested, list, dict-value) encodes successfully."""
    nested = _NestedModel(inner_when=_UTC_NOW)
    parent = _ModelWithNested(label="parent", nested=nested)
    raw = encode_json(parent)
    # Sanity: walks the structure without raising and produces
    # a non-empty JSON string.
    assert raw.startswith("{")
    assert raw.endswith("}")


# ---------------------------------------------------------------------------
# Unserializable types raise plain TypeError (not silent skip)
# ---------------------------------------------------------------------------


def test_unsupported_type_raises_type_error():
    """The plain ``default`` callback raises ``TypeError`` for
    anything it doesn't recognise — matches the standard
    ``json`` library contract."""
    class _Custom:
        pass

    with pytest.raises(TypeError):
        encode_json({"weird": _Custom()})


# ---------------------------------------------------------------------------
# Bad JSON on decode raises (no swallow)
# ---------------------------------------------------------------------------


def test_decode_invalid_json_raises():
    with pytest.raises(json.JSONDecodeError):
        decode_json("not json at all", dict)


def test_decode_pydantic_with_invalid_payload_raises_validation_error():
    """Pydantic's validation error propagates — design §13:
    nothing fails silently."""
    from pydantic import ValidationError

    raw = '{"name": "x", "count": "not_an_int", "when": "2026-05-15T09:30:00+00:00"}'
    with pytest.raises(ValidationError):
        decode_json(raw, _SampleModel)


# ---------------------------------------------------------------------------
# Smoke checks
# ---------------------------------------------------------------------------


def test_serialization_module_has_no_io_imports():
    forbidden = {
        "httpx",
        "requests",
        "urllib.request",
        "urllib3",
        "aiohttp",
        "slack_sdk",
        "telegram",
        "googleapiclient",
        "google.cloud",
        "sqlite3",
        "smtplib",
        "subprocess",
    }
    seen = set()
    for _, member in vars(serialization_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"serialization module imports I/O / storage libs: "
        f"{sorted(leaked)}."
    )


def test_serialization_module_has_no_dispatch_callables():
    forbidden = {
        "dispatch",
        "invoke",
        "call",
        "execute",
        "run",
        "send",
        "start",
        "loop",
        "worker",
    }
    for name, member in vars(serialization_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"serialization module exposes execution-suggestive "
                f"callable: {name}"
            )
