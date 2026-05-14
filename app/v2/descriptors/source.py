"""Source loader contracts.

A source loader is read-only — it fetches bytes from an
external system (Drive, Sheets, Slack history, the local
filesystem) and writes the bytes to disk plus metadata to
SQLite. The metadata shape here mirrors
``app.v2.models.snapshot.SourceSnapshotMetadata`` because that's
what the runtime persists after the load.

Phase 2 ships only the descriptor + I/O contract base classes.
Concrete loaders (``source_drive_file``, ``source_sheets_range``,
``source_slack_thread``, etc.) arrive in design §12 step 9.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.3 + §5.4
- ``docs/PHASE_2_PLAN.md`` §4.2 / §4.3 / §4.4
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.v2.enums import SelectionMethod
from app.v2.tool_tags import ToolCapabilityTag


_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_HEX = re.compile(r"^sha256:[0-9a-f]{64}$")


# Tags incompatible with a read-only source loader. Sources never
# write — those operations route through emit adapters.
_FORBIDDEN_SOURCE_TAGS: frozenset[ToolCapabilityTag] = frozenset(
    {
        ToolCapabilityTag.WRITE_EXTERNAL,
        ToolCapabilityTag.SEND_MESSAGE,
        ToolCapabilityTag.FILESYSTEM_WRITE,
    }
)


class SourceDescriptor(BaseModel):
    """Static registry record for a source loader.

    ``tags`` MUST include ``READ_EXTERNAL`` and MUST NOT include
    any of ``WRITE_EXTERNAL``, ``SEND_MESSAGE``, or
    ``FILESYSTEM_WRITE`` — the source layer is read-only by
    contract.

    ``supports_versioning`` is True for loaders that accept a
    ``version`` pin (Drive revision id, Sheets snapshot id). The
    ExecutionPlan author can rely on this flag at freeze time.

    ``supported_selection_methods`` enumerates which strategies
    the loader knows how to use for picking the fire-time item.
    Author tooling pulls from here to constrain the choice list.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = Field(min_length=8)
    tags: set[ToolCapabilityTag] = Field(min_length=1)
    supports_versioning: bool
    supported_selection_methods: list[SelectionMethod] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _id_is_snake_case(cls, v: str) -> str:
        if not _SNAKE_CASE.match(v):
            raise ValueError(
                f"SourceDescriptor.id must be snake_case "
                f"(^[a-z][a-z0-9_]*$); got {v!r}"
            )
        return v

    @field_validator("tags")
    @classmethod
    def _tags_must_be_read_only(
        cls, v: set[ToolCapabilityTag]
    ) -> set[ToolCapabilityTag]:
        if ToolCapabilityTag.READ_EXTERNAL not in v:
            raise ValueError(
                "SourceDescriptor.tags must include READ_EXTERNAL"
            )
        forbidden = v & _FORBIDDEN_SOURCE_TAGS
        if forbidden:
            names = sorted(t.value for t in forbidden)
            raise ValueError(
                f"SourceDescriptor.tags forbids write-side tags "
                f"(source layer never writes); got: {names}"
            )
        return v


class SourceInputContract(BaseModel):
    """Base request shape every source loader accepts.

    Subclasses add loader-specific fields (e.g. ``drive_file_id``
    for ``source_drive_file``). The runtime guard validates the
    request against the subclass; the base ensures the cross-
    cutting fields are always present.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    as_of_datetime: Optional[datetime] = None


class SourceOutputContract(BaseModel):
    """Response shape every source loader returns.

    The bytes themselves land on disk at ``content_path``
    (relative to the data root). SQLite gets the metadata via
    ``SourceSnapshotMetadata`` after the runtime persists it —
    the field set is intentionally identical so the persistence
    step is a direct ``model_dump`` + insert.
    """

    model_config = ConfigDict(extra="forbid")

    content_hash: str = Field(pattern=_SHA256_HEX.pattern)
    content_path: str = Field(min_length=1)
    content_size: int = Field(ge=0)
    fetched_at: datetime
    source_kind: str = Field(min_length=1)
    source_version: Optional[str] = None
    selection_method: SelectionMethod

    @field_validator("content_path")
    @classmethod
    def _content_path_is_safe_relative(cls, v: str) -> str:
        if v.startswith("/"):
            raise ValueError(
                "SourceOutputContract.content_path must be relative "
                "(no leading '/')"
            )
        parts = v.split("/")
        if any(p == ".." for p in parts):
            raise ValueError(
                "SourceOutputContract.content_path must not contain '..'"
            )
        return v


__all__ = [
    "SourceDescriptor",
    "SourceInputContract",
    "SourceOutputContract",
]
