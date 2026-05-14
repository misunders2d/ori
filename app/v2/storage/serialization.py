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

2. **UTC-aware datetimes only.** Naive datetimes are a common
   bug source (silent UTC reinterpretation across timezones).
   The encoder raises :class:`NaiveDatetimeError` rather than
   silently serializing them. Pydantic models that route their
   datetimes through ``model_dump(mode='json')`` get this for
   free; plain dicts containing raw ``datetime`` objects are
   converted via the ``default`` callback below, which also
   rejects naive values.

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


def encode_json(value: Union[BaseModel, Any]) -> str:
    """Encode ``value`` as a deterministic JSON string.

    Pydantic ``BaseModel`` instances are dumped via
    ``model_dump(mode='json', by_alias=True)`` first, then
    re-serialised through ``json.dumps`` with sorted keys so the
    output is byte-identical for equivalent inputs. Plain Python
    values (dicts, lists, scalars) go straight through
    ``json.dumps``.

    Datetime handling matches both paths: the Pydantic path
    relies on Pydantic's own conversion (which preserves
    tzinfo), and the plain path uses :func:`_json_default`
    above. Naive datetimes raise :class:`NaiveDatetimeError`
    in either case (Pydantic emits an ISO with no offset; the
    runtime test fixture confirms naive Pydantic datetimes
    still surface through the plain re-encode step).
    """
    if isinstance(value, BaseModel):
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
