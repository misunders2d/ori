"""v2 scheduler — source loader layer (phase 10, §12 step 10).

The four phase-1 concrete source loaders + the per-fire
snapshot writer + the per-source cache / fallback /
live-change layer. **Build-the-layer phase**: registered
into the ``SOURCES`` registry and unit-pinned, but NOT
wired into the worker fire path — source-driven schedules
begin firing at step 11 (source templates). See
``docs/PHASE_10_PLAN.md``.

Shared surface (slice 1):
- :mod:`app.v2.sources.errors` — the typed ``SourceError``
  hierarchy (exactly one ``fallback_eligible`` subclass).
- :mod:`app.v2.sources.contract` — :class:`SourceResult`,
  the :class:`SourceLoader` protocol, and the §3.6
  canonical-bytes helper.

Loader registry + ``source_literal`` (slice 2):
- :mod:`app.v2.sources.registry` —
  :class:`SourceLoaderRegistry` + the
  :func:`register_source_loader` seam.
- :mod:`app.v2.sources.literal` — the ``source_literal``
  loader.

Importing this package REGISTERS the built-in loaders into
the production ``SOURCES`` / ``SOURCE_LOADERS`` singletons
ONCE (idempotent — a re-entrant import is a no-op). Tests
that need isolation build fresh registries and call the
per-loader ``register_*`` helpers explicitly.

Snapshot writer + cache + resolver land in slices 4-8.
"""

from __future__ import annotations

from app.v2.registry import SOURCES, SourceRegistry
from app.v2.sources.contract import (
    SourceLoader,
    SourceResult,
    canonical_bytes,
    content_hash_for,
)
from app.v2.sources.errors import (
    SourceAuthError,
    SourceError,
    SourceFetchError,
    SourceParseError,
    SourcePolicyError,
    SourceSecurityError,
)
from app.v2.sources.literal import (
    SOURCE_LITERAL_ID,
    LiteralSource,
    literal_source,
    register_literal,
)
from app.v2.sources.local_file import (
    SOURCE_LOCAL_FILE_ID,
    LocalFileSource,
    local_file_source,
    register_local_file,
)
from app.v2.sources.drive_file import (
    SOURCE_DRIVE_FILE_ID,
    DriveFileReader,
    DriveFileSource,
    drive_file_source,
    register_drive_file,
)
from app.v2.sources.slack_thread import (
    SOURCE_SLACK_THREAD_ID,
    SlackThreadReader,
    SlackThreadSource,
    register_slack_thread,
    slack_thread_source,
)
from app.v2.sources.registry import (
    SOURCE_LOADERS,
    SourceLoaderRegistry,
    register_source_loader,
)
from app.v2.sources.cache import (
    CacheProvenance,
    CacheResolution,
    resolve_source_cached,
)
from app.v2.sources.snapshot_writer import (
    PruneResult,
    SnapshotWriteResult,
    prune_snapshots,
    write_snapshot,
)


#: (source id, paired-registration helper). Each helper
#: takes ``sources=`` / ``loaders=`` and registers BOTH
#: registries atomically (see registry.register_source_loader).
_BUILTIN_SOURCES = (
    (SOURCE_LITERAL_ID, register_literal),
    (SOURCE_LOCAL_FILE_ID, register_local_file),
    (SOURCE_SLACK_THREAD_ID, register_slack_thread),
    (SOURCE_DRIVE_FILE_ID, register_drive_file),
)


def _register_builtin_sources(
    *,
    sources: SourceRegistry = SOURCES,
    loaders: SourceLoaderRegistry = SOURCE_LOADERS,
) -> None:
    """Register every built-in loader into the given
    registries (default: the production singletons).

    Idempotent for the PAIRED state: a loader whose id is
    present in BOTH registries is skipped, so a re-entrant
    / repeated import never raises.

    A HALF state (descriptor present XOR loader present)
    must never be silently no-op'd (codex slice-2 🟡 — that
    preserved a broken split-brain). It can only arise from
    a bug or an out-of-band mutation; surface it loud
    (AI_EDITS rule 13 — nothing fails silently) rather than
    paper over it with an ambiguous "repair".
    """
    for src_id, register_fn in _BUILTIN_SOURCES:
        in_loaders = src_id in loaders
        in_sources = sources.lookup(src_id) is not None
        if in_loaders and in_sources:
            continue  # already paired — idempotent no-op
        if in_loaders != in_sources:
            raise RuntimeError(
                f"split-brain source registration for "
                f"{src_id!r}: descriptor="
                f"{'present' if in_sources else 'missing'}, "
                f"loader="
                f"{'present' if in_loaders else 'missing'}. "
                "Paired registration is atomic — a half "
                "state means a bug or out-of-band mutation; "
                "refusing to silently continue."
            )
        register_fn(sources=sources, loaders=loaders)


_register_builtin_sources()


__all__ = [
    "SourceError",
    "SourceFetchError",
    "SourceAuthError",
    "SourceSecurityError",
    "SourcePolicyError",
    "SourceParseError",
    "SourceResult",
    "SourceLoader",
    "canonical_bytes",
    "content_hash_for",
    "SourceLoaderRegistry",
    "SOURCE_LOADERS",
    "register_source_loader",
    "SOURCE_LITERAL_ID",
    "LiteralSource",
    "literal_source",
    "register_literal",
    "SOURCE_LOCAL_FILE_ID",
    "LocalFileSource",
    "local_file_source",
    "register_local_file",
    "SOURCE_SLACK_THREAD_ID",
    "SlackThreadReader",
    "SlackThreadSource",
    "slack_thread_source",
    "register_slack_thread",
    "SOURCE_DRIVE_FILE_ID",
    "DriveFileReader",
    "DriveFileSource",
    "drive_file_source",
    "register_drive_file",
    "SnapshotWriteResult",
    "PruneResult",
    "write_snapshot",
    "prune_snapshots",
    "CacheProvenance",
    "CacheResolution",
    "resolve_source_cached",
]
