"""V2 scheduler — SourceSnapshotMetadata.

Per-fire snapshot of a LiveSourceRef's content at fire time.
Stored as a row in the ``source_snapshots`` SQLite table pointing
at the on-disk file under
``data/contract_audit/<schedule_id>/<run_id>/sources/<source_id>.json``
(fire_id-keyed paths per the round-3 design correction).

Lets replay reproduce a past fire even when the upstream source
has changed since: the worker reads from the snapshot file
during replay, not the live source.

See ``docs/CONTRACTS_V2_DESIGN.md`` §4.6.4 + ``docs/PHASE_1_PLAN.md``
§4.9.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.v2.enums import SelectionMethod


_CONTENT_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class SourceSnapshotMetadata(BaseModel):
    """Metadata for one per-fire source snapshot.

    The content itself lives on disk; this row identifies it by
    ``content_path`` (relative to repo root, no leading ``/``) and
    cross-checks via ``content_hash``. Replay verifies the hash
    before using the snapshot.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    source_id: str = Field(
        description="The InputSpec id this snapshot is for "
        "(matches ``ExecutionPlan.inputs[].id``).",
    )
    content_hash: str = Field(
        description="``sha256:`` prefix followed by 64 hex chars. "
        "Mismatch on replay = the file was tampered with or "
        "swapped.",
    )
    content_path: str = Field(
        description="Path RELATIVE to repo root. No leading slash; "
        "no ``..`` traversal. Storage layer (phase 3) materialises "
        "this under ``data/contract_audit/...``.",
    )
    content_size: int = Field(ge=0)
    fetched_at: datetime
    source_kind: str = Field(
        description="Loader name that produced this snapshot "
        "(e.g. ``source_drive_file``, ``source_local_file``). "
        "Free-form in phase 1; phase-3 registry tightens.",
    )
    source_version: Optional[str] = Field(
        default=None,
        description="Source-side revision id where supported "
        "(Drive revisionId, Sheets revision, etc.). None when "
        "the source has no native versioning.",
    )
    selection_method: SelectionMethod

    @field_validator("content_hash")
    @classmethod
    def _content_hash_format(cls, v: str) -> str:
        if not _CONTENT_HASH_PATTERN.fullmatch(v):
            raise ValueError(
                "content_hash must match 'sha256:<64-hex-chars>'."
            )
        return v

    @field_validator("content_path")
    @classmethod
    def _content_path_relative(cls, v: str) -> str:
        if v.startswith("/"):
            raise ValueError(
                "content_path must be relative to repo root; got "
                f"absolute path {v!r}."
            )
        if ".." in v.split("/"):
            raise ValueError(
                f"content_path must not contain '..' segments; got {v!r}."
            )
        return v
