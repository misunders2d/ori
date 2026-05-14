"""V2 storage layer — typed CRUD over the v001 SQLite schema.

Slice 1 ships the foundation: connection contract +
JSON / Pydantic serialization glue. Later slices add per-table
CRUD modules on top.

Public re-exports keep the import surface stable across slices.
Callers should import from ``app.v2.storage`` (this package)
rather than the individual modules.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §4.0 (schema), §4.0.4 (invariants)
- ``docs/PHASE_3_PLAN.md`` §3 (connection contract), §6 (JSON)
"""

from app.v2.storage.connection import (
    ConnectionNotReady,
    assert_connection_ready,
)
from app.v2.storage.serialization import (
    NaiveDatetimeError,
    decode_json,
    encode_json,
)


__all__ = [
    "ConnectionNotReady",
    "assert_connection_ready",
    "NaiveDatetimeError",
    "decode_json",
    "encode_json",
]
