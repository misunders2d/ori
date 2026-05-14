"""Emit adapter contracts.

An emit adapter is the side-effect layer of a v2 schedule. It
delivers content to an external destination (Slack post,
Telegram DM, Drive write, Sheets append, email), records the
outcome in the event ledger via the worker, and is the ONLY
path through which a v2 schedule may mutate external state.

Phase 2 ships the descriptor + I/O contract bases. Concrete
adapters arrive in later phases.

The ``EmitInputContract.idempotency_key`` field is validated
against ``app.v2.idempotency.compute_idempotency_key`` so the
descriptor cannot drift from the phase-1 key format.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.4 + §6.2 (emit semantics)
- ``docs/PHASE_2_PLAN.md`` §4.5 / §4.6 / §4.7
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.v2.idempotency import compute_idempotency_key
from app.v2.tool_tags import ToolCapabilityTag


_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


# At least one of these must be set on every emit descriptor —
# an emit that doesn't send a message AND doesn't write external
# state would be a no-op masquerading as a side effect.
_REQUIRED_ANY_EMIT_TAGS: frozenset[ToolCapabilityTag] = frozenset(
    {
        ToolCapabilityTag.SEND_MESSAGE,
        ToolCapabilityTag.WRITE_EXTERNAL,
    }
)


class EmitDescriptor(BaseModel):
    """Static registry record for an emit adapter.

    ``tags`` MUST include at least one of ``SEND_MESSAGE`` or
    ``WRITE_EXTERNAL`` — an emit is by definition a write
    operation.

    ``target_kind`` is the destination class name (``slack``,
    ``telegram``, ``drive``, ``sheets``, ``email``). The
    delivery-fallback chain uses this to find an alternate
    adapter when the primary fails.

    ``supports_native_dedup`` is True iff the destination
    accepts an idempotency token the runtime can pass through
    (Slack ``client_msg_id``, Drive request ids). When False,
    duplicate suppression relies on the event ledger alone.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=8)
    tags: set[ToolCapabilityTag] = Field(min_length=1)
    target_kind: str = Field(min_length=1)
    supports_native_dedup: bool

    @field_validator("id")
    @classmethod
    def _id_is_snake_case(cls, v: str) -> str:
        if not _SNAKE_CASE.match(v):
            raise ValueError(
                f"EmitDescriptor.id must be snake_case "
                f"(^[a-z][a-z0-9_]*$); got {v!r}"
            )
        return v

    @field_validator("target_kind")
    @classmethod
    def _target_kind_is_snake_case(cls, v: str) -> str:
        if not _SNAKE_CASE.match(v):
            raise ValueError(
                f"EmitDescriptor.target_kind must be snake_case; "
                f"got {v!r}"
            )
        return v

    @field_validator("tags")
    @classmethod
    def _tags_must_carry_write_side(
        cls, v: set[ToolCapabilityTag]
    ) -> set[ToolCapabilityTag]:
        if not (v & _REQUIRED_ANY_EMIT_TAGS):
            raise ValueError(
                "EmitDescriptor.tags must include at least one of "
                "SEND_MESSAGE or WRITE_EXTERNAL "
                "(an emit must produce a side effect)"
            )
        return v


class EmitInputContract(BaseModel):
    """Base request shape every emit adapter accepts.

    The runtime calls ``compute_idempotency_key(...)`` and
    passes the result via ``idempotency_key``. The validator
    re-derives the value here and rejects mismatches so a
    hand-rolled or stale key cannot slip past.

    ``payload`` accepts ``dict`` (structured) or ``str``
    (pre-rendered). Subclasses constrain it further (e.g. a
    Slack adapter narrows to a dict with ``text`` and optional
    ``blocks`` keys).
    """

    model_config = ConfigDict(extra="forbid")

    schedule_id: str = Field(min_length=1)
    root_run_id: str = Field(min_length=1)
    emit_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    payload: Union[dict[str, Any], str]

    @model_validator(mode="after")
    def _idempotency_key_matches_components(self) -> "EmitInputContract":
        expected = compute_idempotency_key(
            schedule_id=self.schedule_id,
            root_run_id=self.root_run_id,
            emit_id=self.emit_id,
        )
        if self.idempotency_key != expected:
            raise ValueError(
                "EmitInputContract.idempotency_key does not match "
                "compute_idempotency_key(schedule_id, root_run_id, "
                "emit_id); the runtime must always pass the derived "
                "value, never a hand-rolled one"
            )
        return self


class EmitOutputContract(BaseModel):
    """Response shape every emit adapter returns.

    ``delivered`` is the canonical truthiness flag — the
    runtime writes ``emit_succeeded`` to the ledger iff
    ``delivered is True``.

    Invariant: ``delivered == (error is None)``. The duplication
    is intentional — the storage layer and the audit-log readers
    both rely on the explicit boolean, but a non-empty error
    string is required for any failed delivery so the failure
    monitor has signal to act on.
    """

    model_config = ConfigDict(extra="forbid")

    delivered: bool
    destination_id: Optional[str] = None
    attempted_at: datetime
    error: Optional[str] = None

    @field_validator("attempted_at")
    @classmethod
    def _attempted_at_is_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError(
                "EmitOutputContract.attempted_at must be "
                "timezone-aware (UTC)"
            )
        if v.utcoffset() != timezone.utc.utcoffset(v):
            raise ValueError(
                "EmitOutputContract.attempted_at must be UTC"
            )
        return v

    @model_validator(mode="after")
    def _delivered_matches_error(self) -> "EmitOutputContract":
        if self.delivered and self.error is not None:
            raise ValueError(
                "EmitOutputContract: delivered=True forbids "
                "a non-null error"
            )
        if not self.delivered and (self.error is None or not self.error):
            raise ValueError(
                "EmitOutputContract: delivered=False requires a "
                "non-empty error string"
            )
        return self


__all__ = [
    "EmitDescriptor",
    "EmitInputContract",
    "EmitOutputContract",
]
