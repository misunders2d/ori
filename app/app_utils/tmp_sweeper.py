"""Shared TTL sweeper for transient tmp storage.

One source of truth for "files under ./tmp/ expire after N hours". The list of
swept dirs is explicit — adding a new tmp dir means adding it here too.

Two granularities:
- `sweep_tmp` — file-level mtime sweep for one-shot transient files
  (uploads, exports, gmail attachments, drive downloads, plots).
  TTL via `TMP_STORAGE_TTL_HOURS` (default 24).
- `sweep_scratchpad_sessions` — dir-level mtime sweep for whole session
  scratchpad directories (`tmp/scratchpads/{session_id}/`). Used to
  reclaim space from dead sessions without breaking active ones — a
  session writing recently has its dir mtime refreshed. TTL via
  `SCRATCHPAD_SESSION_TTL_DAYS` (default 7).

Excluded from `sweep_tmp` (handled elsewhere or has its own retention):
- ./tmp/plans is session-scoped state.
- ./tmp/scratchpads is swept by `sweep_scratchpad_sessions` (different
  granularity — whole dirs, longer TTL).
- ./tmp/keepa_cache is a cache with its own TTL semantics.
"""

import logging
import os
import shutil
import time

logger = logging.getLogger(__name__)

# All dirs that hold pure per-request output and can be safely aged out.
_TMP_DIRS: tuple[str, ...] = (
    "./tmp/uploads",
    "./tmp/exports",
    "./tmp/gmail_attachments",
    "./tmp/drive_downloads",
    "./tmp/images",
    "./tmp/plots",
    "./tmp/reports",
)


def _ttl_hours() -> float:
    try:
        return float(os.environ.get("TMP_STORAGE_TTL_HOURS", "24"))
    except ValueError:
        return 24.0


def sweep_tmp(
    dirs: tuple[str, ...] | list[str] | None = None,
    ttl_hours: float | None = None,
) -> int:
    """Remove files older than TTL from transient tmp storage.

    Returns the count of files removed. Missing dirs are skipped silently.
    Safe to call repeatedly — cheap when the dirs are empty or already clean.
    Does not recurse; callers who need recursion can pass nested dir paths.
    """
    targets = dirs if dirs is not None else _TMP_DIRS
    ttl = ttl_hours if ttl_hours is not None else _ttl_hours()
    cutoff = time.time() - (ttl * 3600)
    removed = 0

    for d in targets:
        abs_d = os.path.abspath(d)
        if not os.path.isdir(abs_d):
            continue
        try:
            with os.scandir(abs_d) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    try:
                        if entry.stat().st_mtime < cutoff:
                            os.remove(entry.path)
                            removed += 1
                    except OSError as e:
                        logger.warning("sweep_tmp: failed to remove %s: %s", entry.path, e)
        except OSError as e:
            logger.warning("sweep_tmp: failed to scan %s: %s", abs_d, e)

    if removed:
        logger.info("sweep_tmp: removed %d expired file(s) (ttl=%.1fh)", removed, ttl)
    return removed


_SCRATCHPADS_ROOT = "./tmp/scratchpads"


def _scratchpad_ttl_days() -> float:
    try:
        return float(os.environ.get("SCRATCHPAD_SESSION_TTL_DAYS", "7"))
    except ValueError:
        return 7.0


def sweep_scratchpad_sessions(
    root: str | None = None,
    ttl_days: float | None = None,
) -> int:
    """Remove session-scoped scratchpad dirs older than TTL.

    A "session" is one direct child of `tmp/scratchpads/`. The dir's mtime
    is bumped whenever any pad inside is written, so an actively-used
    session is never swept. Only dead sessions (no writes for ttl_days)
    are removed.

    Returns the count of session dirs removed.
    """
    root = root if root is not None else _SCRATCHPADS_ROOT
    abs_root = os.path.abspath(root)
    if not os.path.isdir(abs_root):
        return 0

    ttl = ttl_days if ttl_days is not None else _scratchpad_ttl_days()
    cutoff = time.time() - (ttl * 24 * 3600)
    removed = 0

    try:
        with os.scandir(abs_root) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        shutil.rmtree(entry.path, ignore_errors=True)
                        removed += 1
                except OSError as e:
                    logger.warning("sweep_scratchpad_sessions: %s: %s", entry.path, e)
    except OSError as e:
        logger.warning("sweep_scratchpad_sessions: failed to scan %s: %s", abs_root, e)

    if removed:
        logger.info(
            "sweep_scratchpad_sessions: removed %d expired session dir(s) (ttl=%.1f days)",
            removed,
            ttl,
        )
    return removed
