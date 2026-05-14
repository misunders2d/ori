"""JSON ↔ Pydantic serialization glue for the v2 storage layer.

The v001 schema stores complex shapes as ``TEXT`` columns
containing JSON (``trigger_json``, ``delivery_json``,
``payload_json``, ``value_json``, ``body_json``). This module
provides one encode / decode pair callers use to translate
between in-memory Pydantic models / Python values and those
columns.

Two design choices:

1. **Deterministic encoding.** Both the Pydantic path and the
   plain-value path produce ``sort_keys=True``,
   ``separators=(",", ":")`` JSON. Equivalent inputs produce
   byte-identical strings — important for hash recomputation
   in tests and for any content-addressed lookup the runtime
   might add later.

2. **Timezone-aware datetimes only.** Naive datetimes are a
   common bug source (silent UTC reinterpretation across
   timezones). The encoder raises :class:`NaiveDatetimeError`
   for both code paths:

   - Plain dict / list: the ``default`` callback fires on
     each raw ``datetime`` and rejects naive ones.
   - Pydantic model: ``model_dump(mode='python', by_alias=True)``
     produces a plain Python representation where datetime
     fields remain as ``datetime`` objects. The encoder walks
     that representation recursively and raises on the first
     naive datetime BEFORE the JSON-mode dump silently
     stringifies it. Catches naive values nested arbitrarily
     deep inside list / dict / sub-model fields.

   This is timezone-aware enforcement, not UTC-only — model
   validators (e.g. ``EmitOutputContract.attempted_at``) own
   the stricter UTC rule for fields that need it.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0.2
- ``docs/PHASE_3_PLAN.md`` §6
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Type, TypeVar, Union

from pydantic import BaseModel


T = TypeVar("T")


class NaiveDatetimeError(ValueError):
    """Raised when the encoder sees a naive (tzinfo=None)
    ``datetime``.

    Subclass of ``ValueError`` because the input is shaped
    correctly (it IS a datetime) — only the tz attribute is
    wrong. Callers should attach tzinfo (typically
    ``datetime.timezone.utc``) before encoding.
    """


def _json_default(value: Any) -> Any:
    """``json.dumps`` ``default`` callback.

    Handles ``datetime`` by producing the ISO 8601 string of a
    timezone-aware value, raising :class:`NaiveDatetimeError`
    for naive ones. Anything else raises ``TypeError`` per the
    standard ``json`` library contract.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise NaiveDatetimeError(
                "naive datetime cannot be serialised — attach "
                "tzinfo (typically datetime.timezone.utc) before "
                "encoding."
            )
        return value.isoformat()
    raise TypeError(
        f"{type(value).__name__} is not JSON-serialisable"
    )


def _assert_no_naive_datetime(value: Any, path: str = "<root>") -> None:
    """Walk a Python value and raise :class:`NaiveDatetimeError`
    on the first naive datetime encountered.

    Used on the Pydantic encode path against the result of
    ``model_dump(mode='python', by_alias=True)``: that
    representation preserves datetime objects (mode='json'
    silently stringifies them and would let naive datetimes
    slip through). We walk it BEFORE the JSON-mode dump so
    naive values fail loud at the storage boundary regardless
    of how deeply they're nested.

    ``path`` is a JSONPath-style trace so the error message
    pinpoints which field carried the naive value.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise NaiveDatetimeError(
                f"naive datetime at {path}: {value!r} — attach "
                "tzinfo (typically datetime.timezone.utc) before "
                "encoding."
            )
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_no_naive_datetime(v, f"{path}.{k}")
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _assert_no_naive_datetime(v, f"{path}[{i}]")
        return
    # Scalars (None, str, int, float, bool, bytes) and any
    # opaque types Pydantic returns from custom serializers:
    # skip. Naive datetimes only live in datetime objects.


def encode_json(value: Union[BaseModel, Any]) -> str:
    """Encode ``value`` as a deterministic JSON string.

    Pydantic ``BaseModel`` instances go through a two-step:

    1. ``model_dump(mode='python', by_alias=True)`` → walk the
       result with :func:`_assert_no_naive_datetime` to reject
       any naive datetime arbitrarily deep in the structure.
    2. ``model_dump(mode='json', by_alias=True)`` → final JSON
       representation, re-serialised via ``json.dumps`` with
       sorted keys + compact separators so equivalent inputs
       produce byte-identical strings.

    Plain Python values (dicts, lists, scalars) skip the walk
    because :func:`_json_default` handles raw datetimes on the
    fly during ``json.dumps`` (it sees each ``datetime`` before
    serialisation and rejects naive ones).
    """
    if isinstance(value, BaseModel):
        py_dump = value.model_dump(mode="python", by_alias=True)
        _assert_no_naive_datetime(py_dump)
        body = value.model_dump(mode="json", by_alias=True)
        return json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def decode_json(raw: str, target_type: Type[T]) -> T:
    """Decode a JSON string into ``target_type``.

    ``target_type`` is a ``BaseModel`` subclass → returns a
    validated model instance via ``model_validate_json``.
    Otherwise → returns the result of ``json.loads`` (the
    caller is responsible for the runtime shape).

    Pydantic raises ``ValidationError`` on shape mismatch; the
    plain path raises ``json.JSONDecodeError`` on bad JSON.
    The helper does not swallow either — design §13.
    """
    if isinstance(target_type, type) and issubclass(target_type, BaseModel):
        return target_type.model_validate_json(raw)  # type: ignore[return-value]
    return json.loads(raw)  # type: ignore[return-value]


__all__ = ["NaiveDatetimeError", "decode_json", "encode_json"]
