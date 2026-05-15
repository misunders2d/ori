"""V2 scheduler — dry-run handshake storage.

Phase 8 slice 1 per ``docs/PHASE_8_PLAN.md`` §3.1 + §5.1.

Three public surfaces:

- :class:`DryRunMode` — the three documented dry-run modes.
  Phase 8 implements ``VALIDATE_ONLY`` in full;
  ``MOCKED_INPUTS`` / ``REAL`` are stubbed in
  ``schedule_dry_run`` (phase 8 slice 2) and return a
  ``mode_not_implemented_in_phase_8`` validation failure
  until phases 10 / 12 ship the ``real`` body.

- :class:`HandshakeRecord` — Pydantic v2 model carrying the
  dry-run handshake state freeze + commit gate on. Every
  ``datetime`` field is enforced tz-aware UTC via the local
  :func:`_require_utc` helper (mirror of the phase-6
  registry_cache helper of the same name).

- :class:`HandshakeStore` — file-backed JSON persistence
  under ``tmp/v2_handshakes/<session_id>/<draft_id>.json``.
  Mirrors the phase-7 :class:`app.v2.authoring.drafts.DraftStore`
  pattern exactly:

  - Slug regex (``^[A-Za-z0-9_-]{1,128}$``) for both
    ``session_id`` and ``draft_id``; anything else →
    :class:`ValueError` BEFORE any I/O.
  - ``Path.resolve().is_relative_to(base.resolve())`` fence
    after building the candidate path so symlink escapes the
    slug regex would accept still raise.
  - :func:`tempfile.mkstemp` + :func:`os.rename` atomic write.
    Same-pid concurrent writes get distinct tmp names so they
    never collide; a crash mid-rename preserves the previous
    record and leaves a ``.tmp.*`` artifact for forensics.
  - :meth:`delete` is idempotent (missing file is not an
    error).

References:
- ``docs/PHASE_8_PLAN.md`` §3.1 / §5.1
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.6 (60s handshake window)
"""

from __future__ import annotations

import os
import pathlib
import re
import tempfile
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_HANDSHAKE_WINDOW_SECONDS = 60  # design §5.6
_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
DEFAULT_HANDSHAKE_BASE: pathlib.Path = pathlib.Path("tmp/v2_handshakes")


# ---------------------------------------------------------------------------
# DryRunMode
# ---------------------------------------------------------------------------


class DryRunMode(str, Enum):
    """Dry-run mode per design §5.6.

    Phase 8 implements ``VALIDATE_ONLY``; the other two modes
    depend on ExecutionPlan + source loaders (phases 10 / 12)
    and return ``mode_not_implemented_in_phase_8`` from
    :func:`schedule_dry_run` until those phases ship.
    """

    VALIDATE_ONLY = "validate_only"
    MOCKED_INPUTS = "mocked_inputs"
    REAL = "real"


# ---------------------------------------------------------------------------
# Shared UTC validator — mirror of phase-6 registry_cache._require_utc
# ---------------------------------------------------------------------------


def _require_utc(value: datetime) -> datetime:
    """Reject naive datetimes AND non-UTC offsets.

    Same shape as the phase-6 registry_cache helper of the
    same name; duplicated locally rather than cross-imported
    so the authoring package stays self-contained.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "handshake datetime must be tz-aware UTC "
            "(got naive datetime)"
        )
    if value.utcoffset() != timedelta(0):
        raise ValueError(
            "handshake datetime must be UTC "
            f"(got utcoffset={value.utcoffset()!r}); convert via "
            "value.astimezone(timezone.utc) before persistence"
        )
    return value


# ---------------------------------------------------------------------------
# HandshakeRecord
# ---------------------------------------------------------------------------


class HandshakeRecord(BaseModel):
    """Persisted dry-run handshake state. ``schedule_freeze``
    and ``schedule_draft_commit`` gate on this record's
    freshness + hash."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    session_id: str
    body_hash: str
    mode: DryRunMode
    as_of_datetime: Optional[datetime] = None
    recorded_at: datetime
    expires_at: datetime

    @field_validator("recorded_at", "expires_at", "as_of_datetime")
    @classmethod
    def _utc_only(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return v
        return _require_utc(v)

    def is_expired(self, *, now: datetime) -> bool:
        """``True`` when ``now`` strictly exceeds
        ``expires_at``. The boundary itself (``now ==
        expires_at``) is still fresh."""
        return now > self.expires_at


# ---------------------------------------------------------------------------
# HandshakeStore — slug + resolve() fence + atomic write
# ---------------------------------------------------------------------------


def _validate_slug(value: str, label: str) -> None:
    """Reject any id segment that does not match the slug
    regex (mirror DraftStore L41 fix)."""
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}: not a string")
    if not _SLUG_PATTERN.fullmatch(value):
        raise ValueError(
            f"invalid {label} {value!r}: must match "
            f"{_SLUG_PATTERN.pattern}"
        )


class HandshakeStore:
    """File-backed handshake persistence with a slug +
    resolve() fence against path traversal."""

    def __init__(self, *, base: Optional[pathlib.Path] = None) -> None:
        self.base: pathlib.Path = (
            base if base is not None else DEFAULT_HANDSHAKE_BASE
        )

    # ----- path resolution -----

    def _resolve(self, session_id: str, draft_id: str) -> pathlib.Path:
        """Resolve the on-disk path for a handshake. Validates
        the slugs AND verifies the resolved path stays inside
        the base directory."""
        _validate_slug(session_id, "session_id")
        _validate_slug(draft_id, "draft_id")

        candidate = self.base / session_id / f"{draft_id}.json"
        resolved = candidate.resolve()
        base_resolved = self.base.resolve()
        if not resolved.is_relative_to(base_resolved):
            raise ValueError(
                f"handshake path {resolved} escapes base "
                f"{base_resolved}"
            )
        return candidate

    # ----- CRUD -----

    def read(
        self, session_id: str, draft_id: str
    ) -> HandshakeRecord:
        """Load a handshake from disk. Raises
        :class:`FileNotFoundError` when the file is absent;
        callers map to the appropriate ``ToolResponse``
        validation_failed code (``dry_run_required`` for
        freeze + commit)."""
        path = self._resolve(session_id, draft_id)
        raw = path.read_text(encoding="utf-8")
        return HandshakeRecord.model_validate_json(raw)

    def write(
        self, session_id: str, record: HandshakeRecord
    ) -> None:
        """Persist a handshake atomically.

        Uses :func:`tempfile.mkstemp` to allocate a unique
        tmp path in the target's parent directory, writes the
        JSON, then :func:`os.rename` over the target. Same-pid
        concurrent writes do not collide.
        """
        path = self._resolve(session_id, record.draft_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.name}.tmp.",
            dir=str(path.parent),
        )
        os.close(tmp_fd)
        tmp_path = pathlib.Path(tmp_name)
        tmp_path.write_text(
            record.model_dump_json(indent=2),
            encoding="utf-8",
        )
        os.rename(tmp_path, path)

    def delete(
        self, session_id: str, draft_id: str
    ) -> None:
        """Remove the handshake file. Idempotent — missing
        file is not an error."""
        path = self._resolve(session_id, draft_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return


__all__ = [
    "DEFAULT_HANDSHAKE_BASE",
    "DryRunMode",
    "HandshakeRecord",
    "HandshakeStore",
]
