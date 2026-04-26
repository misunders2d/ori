"""Shared TTL sweeper for transient tmp storage.

One source of truth for "files under ./tmp/ expire after N hours". The list of
swept dirs is explicit — adding a new tmp dir means adding it here too.

Deliberately excluded:
- ./tmp/plans and ./tmp/scratchpads are session-scoped state (tied to session
  lifecycle, not wall-clock TTL). Deleting them mid-session breaks the agent.
- ./tmp/keepa_cache is a cache with its own retention semantics.

Env var TMP_STORAGE_TTL_HOURS (default 24) controls retention.
"""

import logging
import os
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
