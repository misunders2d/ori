"""V2 scheduler — authoring draft storage.

Phase 7 slice 1 per ``docs/PHASE_7_PLAN.md`` §3.2 + §5.2.

Two public surfaces:

- :class:`ScheduleSpecDraft` — relaxed superset of
  :class:`app.v2.models.schedule.ScheduleSpec`. Every
  required field is ``Optional`` while authoring is in
  flight. :meth:`missing_required_fields` enumerates the
  unset required fields; :meth:`to_spec` converts to a
  fully-typed ``ScheduleSpec`` (calling ``with_fresh_hash``
  to populate the canonical hash).
- :class:`DraftStore` — file-backed persistence under
  ``tmp/v2_drafts/<session_id>/<draft_id>.json``.

Security fence (round-3 reviewer L41):

- ``session_id`` / ``draft_id`` must match
  :data:`_SLUG_PATTERN` (``^[A-Za-z0-9_-]{1,128}$``).
  Anything else → :class:`ValueError` BEFORE any I/O.
- After building the target path, ``resolved =
  candidate.resolve()`` then
  ``resolved.is_relative_to(base.resolve())`` MUST be True.
  Catches symlink escapes the regex alone misses.

Atomic write (round-3 reviewer mirror of phase-6 fix):

- :meth:`DraftStore.write` uses
  :func:`tempfile.mkstemp` to allocate a unique tmp path in
  the target's parent directory, writes the JSON, then
  :func:`os.rename` the tmp file over the target. POSIX
  rename is atomic on the local filesystem. Same-pid
  concurrent writes get distinct tmp names so they never
  collide.

References:
- ``docs/PHASE_7_PLAN.md`` §3.2 / §5.2
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.1
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import tempfile
from datetime import datetime
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict

from app.v2.enums import ScheduleStatus
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import Trigger


_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
DEFAULT_DRAFT_BASE: pathlib.Path = pathlib.Path("tmp/v2_drafts")


# ---------------------------------------------------------------------------
# ScheduleSpecDraft — relaxed-optional superset
# ---------------------------------------------------------------------------


class ScheduleSpecDraft(BaseModel):
    """Relaxed superset of :class:`ScheduleSpec` — every
    required field is ``Optional`` while authoring is in
    flight. ``id`` is still required (it's the draft's
    primary key on disk)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: Optional[str] = None
    owner: Optional[UserRef] = None
    trigger: Optional[Trigger] = None
    delivery: Optional[Delivery] = None
    failure: Optional[FailurePolicy] = None
    audit: AuditPolicy = AuditPolicy()
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    execution_plan_hash: Optional[str] = None

    def missing_required_fields(self) -> list[str]:
        """Return the names of unset required-on-spec fields
        in declaration order. ``id`` is always present (the
        draft cannot exist without one); the other four
        required ScheduleSpec fields are the variables."""
        missing: list[str] = []
        if self.description is None:
            missing.append("description")
        if self.owner is None:
            missing.append("owner")
        if self.trigger is None:
            missing.append("trigger")
        if self.delivery is None:
            missing.append("delivery")
        if self.failure is None:
            missing.append("failure")
        return missing

    def to_spec(
        self,
        *,
        clock: Callable[[], datetime],
    ) -> ScheduleSpec:
        """Build a fully-typed :class:`ScheduleSpec` from this
        draft. Calls ``with_fresh_hash`` so the result is a
        frozen, hash-populated spec ready for validation.

        ``clock()`` populates ``authored_at`` (ISO 8601 UTC).
        The helper imports no clock itself — phase-5 hard
        rule 10 keeps ``_defaults.py`` the sole
        ``datetime.now`` site. AST pin in
        ``test_authoring_drafts.py`` enforces this.

        Raises :class:`ValueError` when any required field is
        unset (caller should check
        :meth:`missing_required_fields` first).
        """
        missing = self.missing_required_fields()
        if missing:
            raise ValueError(
                f"cannot build ScheduleSpec from draft "
                f"{self.id!r}: missing required fields "
                f"{missing!r}"
            )

        # Type-guard: pydantic still sees Optional, narrow
        # the locals for mypy / human readers.
        assert self.description is not None
        assert self.owner is not None
        assert self.trigger is not None
        assert self.delivery is not None
        assert self.failure is not None

        authored_at = clock().isoformat()
        spec = ScheduleSpec(
            id=self.id,
            description=self.description,
            owner=self.owner,
            trigger=self.trigger,
            delivery=self.delivery,
            failure=self.failure,
            audit=self.audit,
            status=self.status,
            execution_plan_hash=self.execution_plan_hash,
            authored_at=authored_at,
        )
        return spec.with_fresh_hash()


# ---------------------------------------------------------------------------
# DraftStore — path fence + atomic file-backed CRUD
# ---------------------------------------------------------------------------


def _validate_slug(value: str, label: str) -> None:
    """Reject any id segment that does not match the slug
    regex (round-3 reviewer L41 fix). Empty strings, paths
    with ``/`` or ``\\``, oversize strings, and traversal
    fragments all fail."""
    if not isinstance(value, str):
        raise ValueError(f"invalid {label}: not a string")
    if not _SLUG_PATTERN.fullmatch(value):
        raise ValueError(
            f"invalid {label} {value!r}: must match "
            f"{_SLUG_PATTERN.pattern}"
        )


class DraftStore:
    """File-backed draft persistence with a slug + resolve()
    fence against path traversal."""

    def __init__(self, *, base: Optional[pathlib.Path] = None) -> None:
        self.base: pathlib.Path = (
            base if base is not None else DEFAULT_DRAFT_BASE
        )

    # ----- path resolution -----

    def _resolve(self, session_id: str, draft_id: str) -> pathlib.Path:
        """Resolve the on-disk path for a draft. Validates
        the slugs AND verifies the resolved path stays
        inside the base directory."""
        _validate_slug(session_id, "session_id")
        _validate_slug(draft_id, "draft_id")

        # Build the candidate path. We DO call resolve() on
        # both ends so symlinks are dereferenced.
        candidate = self.base / session_id / f"{draft_id}.json"
        resolved = candidate.resolve()
        base_resolved = self.base.resolve()
        if not resolved.is_relative_to(base_resolved):
            raise ValueError(
                f"draft path {resolved} escapes base "
                f"{base_resolved}"
            )
        return candidate

    def _session_dir(self, session_id: str) -> pathlib.Path:
        _validate_slug(session_id, "session_id")
        candidate = self.base / session_id
        resolved = candidate.resolve()
        base_resolved = self.base.resolve()
        if not resolved.is_relative_to(base_resolved):
            raise ValueError(
                f"session dir {resolved} escapes base "
                f"{base_resolved}"
            )
        return candidate

    # ----- CRUD -----

    def read(
        self, session_id: str, draft_id: str
    ) -> ScheduleSpecDraft:
        """Load a draft from disk. Raises
        :class:`FileNotFoundError` when the file is absent;
        callers wrap as :class:`ToolResponse.not_found`."""
        path = self._resolve(session_id, draft_id)
        raw = path.read_text(encoding="utf-8")
        return ScheduleSpecDraft.model_validate_json(raw)

    def write(
        self, session_id: str, draft: ScheduleSpecDraft
    ) -> None:
        """Persist a draft atomically.

        Uses :func:`tempfile.mkstemp` to allocate a unique
        tmp path in the target's parent directory, writes
        the JSON, then :func:`os.rename` over the target.
        Same-pid concurrent writes do not collide.
        """
        path = self._resolve(session_id, draft.id)
        path.parent.mkdir(parents=True, exist_ok=True)

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f"{path.name}.tmp.",
            dir=str(path.parent),
        )
        os.close(tmp_fd)
        tmp_path = pathlib.Path(tmp_name)
        tmp_path.write_text(
            draft.model_dump_json(indent=2),
            encoding="utf-8",
        )
        os.rename(tmp_path, path)

    def delete(
        self, session_id: str, draft_id: str
    ) -> None:
        """Remove the draft file. Idempotent — missing
        file is not an error."""
        path = self._resolve(session_id, draft_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return

    def list_ids(self, session_id: str) -> list[str]:
        """Return draft ids for ``session_id`` in
        lexicographic order. Empty list when the session
        directory is absent."""
        session_dir = self._session_dir(session_id)
        if not session_dir.is_dir():
            return []
        return sorted(
            p.stem
            for p in session_dir.iterdir()
            if p.suffix == ".json" and p.is_file()
        )


__all__ = [
    "DEFAULT_DRAFT_BASE",
    "DraftStore",
    "ScheduleSpecDraft",
]
