"""v2 scheduler — per-fire source snapshot writer + retention.

Phase 10 slice 4 per ``docs/PHASE_10_PLAN.md`` §3.4 +
``docs/CONTRACTS_V2_DESIGN.md`` §5.3.5.

Write path (:func:`write_snapshot`):
- The on-disk artifact is ``<source_id>.bin`` holding the
  §3.6 ``content_bytes`` VERBATIM — NO JSON envelope, NO
  metadata in the file. ``sha256(file) == content_hash``
  re-verifies with zero parsing for EVERY kind incl.
  binary.
- Content-addressed dedup: when ``audit.dedup_by_content_hash``
  and a row with the same ``content_hash`` already exists,
  reuse its ``content_path`` — write only the new
  ``source_snapshots`` row, no second file.
- ``on_oversize`` (``content_size > audit.max_snapshot_bytes``)
  — explicit, no silent fallback:
  ``fail_and_alert`` → :class:`SourcePolicyError`
  (non-fallback, NO row/file); ``store_pointer_only`` →
  row with empty ``content_path``, no body;
  ``redact_and_store`` → ``audit.redact_fields`` stripped,
  re-measured, still oversize ⇒ ``fail_and_alert``;
  ``hash_only_no_replay`` → row + hash only, body
  discarded.

Retention (:func:`prune_snapshots`):
- COMMIT the row deletes FIRST (one ``transaction(conn)``),
  THEN best-effort file unlink AFTER commit on a FRESH
  connection with a re-query. Files are NEVER unlinked
  inside the delete transaction — a rollback would restore
  rows pointing at already-deleted files (= data loss;
  plan 🔴). An orphaned file (row gone, unlink failed) is
  harmless and counted; a deleted file with a live row is
  structurally impossible. Dedup-shared files are unlinked
  only when their LAST referencing row is pruned.

Build-the-layer: NOT wired into the worker fire path
(slice 8 resolver / step-11 templates do that).
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
import sqlite3

from app.v2.enums import OnOversizePolicy
from app.v2.models.common import AuditPolicy
from app.v2.models.snapshot import SourceSnapshotMetadata
from app.v2.sources.contract import (
    SourceResult,
    canonical_bytes,
    content_hash_for,
)
from app.v2.sources.errors import SourcePolicyError
from app.v2.storage.source_snapshots import (
    content_path_referenced,
    delete_snapshot,
    insert_snapshot,
    list_snapshots_by_hash,
    list_snapshots_for_schedule_source,
)
from app.v2.storage.transactions import transaction


_logger = logging.getLogger(__name__)

# Repo root = the dir containing ``app/`` —
# app/v2/sources/snapshot_writer.py → parents[3].
_REPO_ROOT = Path(__file__).resolve().parents[3]

_SNAPSHOT_SUBDIR = "data/contract_audit"

#: Empty content_path = "no body on disk" sentinel
#: (store_pointer_only / hash_only_no_replay). The
#: SourceSnapshotMetadata validator accepts "" (not
#: absolute, no ``..``).
_NO_BODY = ""


@dataclass(frozen=True)
class SnapshotWriteResult:
    content_path: str
    content_hash: str
    content_size: int
    deduped: bool
    on_oversize_action: Optional[str]
    row: SourceSnapshotMetadata


@dataclass(frozen=True)
class PruneResult:
    rows_deleted: int
    files_unlinked: int
    files_kept_shared: int
    orphans_logged: int


def _rel_content_path(
    *, schedule_id: str, run_id: str, source_id: str
) -> str:
    """Repo-root-relative POSIX path for the snapshot file
    (validator-safe: not absolute, no ``..``). Absolute
    location = ``repo_root / content_path``."""
    return (
        f"{_SNAPSHOT_SUBDIR}/{schedule_id}/{run_id}/"
        f"sources/{source_id}.bin"
    )


def _redact(value: Any, fields: frozenset[str]) -> Any:
    """Recursively drop any mapping key whose name is in
    ``fields``. Lists recurse element-wise. Non-container
    values pass through unchanged (a text / bytes payload
    has nothing to redact)."""
    if isinstance(value, dict):
        return {
            k: _redact(v, fields)
            for k, v in value.items()
            if k not in fields
        }
    if isinstance(value, list):
        return [_redact(v, fields) for v in value]
    return value


def _atomic_write(abs_path: Path, data: bytes) -> None:
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(abs_path.parent), prefix=".snap-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, abs_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_snapshot(
    conn: sqlite3.Connection,
    *,
    result: SourceResult,
    schedule_id: str,
    run_id: str,
    audit: AuditPolicy,
    repo_root: Path = _REPO_ROOT,
) -> SnapshotWriteResult:
    """Persist one per-fire snapshot + its
    ``source_snapshots`` row. See module docstring for the
    on_oversize / dedup contract."""
    content = result.content
    content_bytes = result.content_bytes
    content_hash = result.content_hash
    on_oversize_action: Optional[str] = None

    size = len(content_bytes)
    if size > audit.max_snapshot_bytes:
        policy = audit.on_oversize
        if policy == OnOversizePolicy.FAIL_AND_ALERT:
            raise SourcePolicyError(
                f"snapshot is {size} bytes > max "
                f"{audit.max_snapshot_bytes} "
                "(on_oversize=fail_and_alert)",
                code="snapshot_oversize",
            )
        if policy == OnOversizePolicy.REDACT_AND_STORE:
            redacted = _redact(
                content, frozenset(audit.redact_fields)
            )
            content = redacted
            content_bytes = canonical_bytes("json", redacted)
            content_hash = content_hash_for(content_bytes)
            size = len(content_bytes)
            if size > audit.max_snapshot_bytes:
                # Redaction did not bring it under the cap
                # → fall through to fail_and_alert (explicit,
                # no silent store).
                raise SourcePolicyError(
                    f"snapshot still {size} bytes > max "
                    f"{audit.max_snapshot_bytes} after "
                    "redact_and_store",
                    code="snapshot_oversize_after_redact",
                )
            on_oversize_action = "redact_and_store"
        elif policy == OnOversizePolicy.STORE_POINTER_ONLY:
            on_oversize_action = "store_pointer_only"
        elif policy == OnOversizePolicy.HASH_ONLY_NO_REPLAY:
            on_oversize_action = "hash_only_no_replay"

    write_body = on_oversize_action not in (
        "store_pointer_only",
        "hash_only_no_replay",
    )

    deduped = False
    if write_body:
        rel_path = _rel_content_path(
            schedule_id=schedule_id,
            run_id=run_id,
            source_id=result.source_id,
        )
        if audit.dedup_by_content_hash:
            existing = list_snapshots_by_hash(conn, content_hash)
            if existing:
                # Content-addressed: reuse the prior file,
                # write only the new row.
                rel_path = existing[0].content_path
                deduped = True
        if not deduped:
            _atomic_write(repo_root / rel_path, content_bytes)
    else:
        rel_path = _NO_BODY

    meta = SourceSnapshotMetadata(
        run_id=run_id,
        source_id=result.source_id,
        content_hash=content_hash,
        content_path=rel_path,
        content_size=size,
        fetched_at=result.fetched_at,
        source_kind=result.source_kind,
        source_version=result.source_version,
        selection_method=result.selection_method,
    )
    # Commit the row (the file, if any, is already written
    # — an insert failure leaves an orphan file, harmless +
    # content-addressed). The body is written BEFORE the
    # row so a reader never sees a row pointing at a missing
    # file.
    with transaction(conn):
        insert_snapshot(conn, meta)

    return SnapshotWriteResult(
        content_path=rel_path,
        content_hash=content_hash,
        content_size=size,
        deduped=deduped,
        on_oversize_action=on_oversize_action,
        row=meta,
    )


def prune_snapshots(
    conn: sqlite3.Connection,
    conn_factory: Callable[[], sqlite3.Connection],
    *,
    schedule_id: str,
    source_id: str,
    keep_last_n: int,
    repo_root: Path = _REPO_ROOT,
) -> PruneResult:
    """Retain the newest ``keep_last_n`` snapshots for
    ``(schedule_id, source_id)``; delete the rest.

    Ordering is strict (plan 🔴): the row DELETEs commit in
    ONE ``transaction(conn)``; ONLY AFTER the commit, on a
    FRESH connection from ``conn_factory``, each pruned
    ``content_path`` is re-queried and best-effort unlinked
    iff no surviving row still references it. A file unlink
    failure post-commit is logged + counted as an orphan
    (harmless); a deleted file with a live row is
    structurally impossible.
    """
    with transaction(conn):
        rows = list_snapshots_for_schedule_source(
            conn, schedule_id=schedule_id, source_id=source_id
        )
        prune = rows[keep_last_n:] if keep_last_n >= 0 else []
        for run_id, src_id, _cp in prune:
            delete_snapshot(conn, run_id=run_id, source_id=src_id)
    # ---- committed; safe to touch the filesystem now ----

    rows_deleted = len(prune)
    # Distinct non-empty paths (skip the pointer/hash-only
    # sentinel — those never wrote a body).
    pruned_paths = {cp for (_, _, cp) in prune if cp}

    files_unlinked = 0
    files_kept_shared = 0
    orphans_logged = 0

    if pruned_paths:
        fresh = conn_factory()
        try:
            for cp in pruned_paths:
                if content_path_referenced(fresh, cp):
                    files_kept_shared += 1
                    continue
                abs_path = repo_root / cp
                try:
                    abs_path.unlink()
                    files_unlinked += 1
                except FileNotFoundError:
                    # Already gone — the desired end state.
                    files_unlinked += 1
                except OSError as exc:
                    orphans_logged += 1
                    _logger.warning(
                        "snapshot retention: could not unlink "
                        "orphan %s: %s (harmless — row already "
                        "deleted)",
                        abs_path,
                        exc,
                    )
        finally:
            fresh.close()

    return PruneResult(
        rows_deleted=rows_deleted,
        files_unlinked=files_unlinked,
        files_kept_shared=files_kept_shared,
        orphans_logged=orphans_logged,
    )


__all__ = [
    "SnapshotWriteResult",
    "PruneResult",
    "write_snapshot",
    "prune_snapshots",
]
