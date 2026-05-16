"""v2 scheduler — per-source cache + fallback resolution.

Phase 10 slice 5 per ``docs/PHASE_10_PLAN.md`` §1.4 / §3.3
/ §3.5 + ``docs/CONTRACTS_V2_DESIGN.md`` §5.3.2.

The per-source "cache" for a LiveSourceRef is the
content-addressed ``source_snapshots`` history + its
on-disk ``.bin`` bodies — the last materialised snapshot
within a freshness window IS the cache. Two distinct knobs
(plan Q2):

- ``cache.cache_ttl_seconds`` — cross-fire cache: a
  materialised snapshot newer than this is served WITHOUT
  re-invoking the loader (cache hit).
- ``cache.stale_max_age_seconds`` — the maximum age a
  last-good snapshot may have to still satisfy a
  ``use_last_good_snapshot`` fallback after a fetch
  failure.

**HARD invariant (codex slice-5 crux).** Fallback
eligibility is the §3.5 ``fallback_eligible`` ClassVar:
ONLY :class:`SourceFetchError` (transient network) is
eligible. :class:`SourceAuthError` /
:class:`SourceSecurityError` / :class:`SourcePolicyError`
/ :class:`SourceParseError` are NON-fallback — they NEVER
consult the cache, NEVER consult ``fallback_policy``,
NEVER write / refresh the cache, and propagate straight
out (the slice-8 resolver turns them into
``SOURCE_FAILED``). An auth failure with a warm last-good
snapshot present still fails — it must never serve a
stale cached snapshot or a default, and must never poison
the cache.

Cache-hit vs the non-fallback invariant (plan §1.4 / §9c
/ design §5.3.2): a within-``cache_ttl_seconds`` verified
hit is a deliberate **no-probe window** — the captured
snapshot IS the source and the loader is NOT invoked, so
a ``CACHE_HIT`` legitimately does not re-auth (no fetch ⇒
no live auth to mask). The §3.5 non-fallback invariant
governs the **post-fetch** decision only.
``cache_ttl_seconds == 0`` is the explicit security
opt-out: the cross-fire cache-hit path is skipped
unconditionally, the loader is probed EVERY fire, and a
live auth/security failure surfaces every time (never
masked by a stale entry).

A tampered / missing cached ``.bin`` (hash mismatch /
file gone) is treated as NO cache — never served.

Build-the-layer: this is the cache+fallback CORE. Event
emission (``SOURCE_RESOLVED`` / ``SOURCE_FAILED`` / drift)
and the worker wiring are the slice-8 resolver's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
import sqlite3

from pydantic.types import JsonValue

from app.v2.enums import SourceFallbackPolicy
from app.v2.models.common import AuditPolicy
from app.v2.models.snapshot import SourceSnapshotMetadata
from app.v2.models.source_ref import SourceRefSpec
from app.v2.sources.contract import (
    SourceLoader,
    SourceResult,
    content_hash_for,
)
from app.v2.sources.errors import SourceError, SourceFetchError
from app.v2.sources.snapshot_writer import (
    SnapshotWriteResult,
    write_snapshot,
)
from app.v2.storage.source_snapshots import (
    get_snapshot,
    list_snapshots_for_schedule_source,
)

# repo root = dir containing app/ (app/v2/sources/cache.py)
_REPO_ROOT = Path(__file__).resolve().parents[3]


class CacheProvenance(str, Enum):
    """How the served content was obtained."""

    FRESH = "fresh"  # loader fetched; new snapshot written
    CACHE_HIT = "cache_hit"  # within cache_ttl; loader skipped
    FALLBACK_LAST_GOOD = "fallback_last_good"  # fetch failed
    FALLBACK_DEFAULT = "fallback_default"  # fetch failed


@dataclass(frozen=True)
class CacheResolution:
    """The outcome of a cache+fallback resolution. Carries
    the served body / metadata; event emission is the
    slice-8 resolver's job."""

    provenance: CacheProvenance
    content_bytes: Optional[bytes] = None
    content_hash: Optional[str] = None
    snapshot: Optional[SourceSnapshotMetadata] = None
    fresh_result: Optional[SourceResult] = None
    fresh_write: Optional[SnapshotWriteResult] = None
    default_value: Optional[JsonValue] = None


def _newest_materialised_snapshot(
    conn: sqlite3.Connection,
    *,
    schedule_id: str,
    source_id: str,
) -> Optional[SourceSnapshotMetadata]:
    """Newest snapshot for ``(schedule_id, source_id)``
    that actually materialised a body (``content_path`` !=
    ""). Pointer-only / hash-only rows are skipped — they
    have no servable cache body."""
    for run_id, src_id, content_path in (
        list_snapshots_for_schedule_source(
            conn, schedule_id=schedule_id, source_id=source_id
        )
    ):
        if not content_path:
            continue
        meta = get_snapshot(conn, run_id=run_id, source_id=src_id)
        if meta is not None:
            return meta
    return None


def _read_verified_body(
    repo_root: Path, meta: SourceSnapshotMetadata
) -> Optional[bytes]:
    """Read the cached ``.bin`` and verify
    ``sha256(file) == meta.content_hash``. A missing or
    tampered file → ``None`` (never served)."""
    if not meta.content_path:
        return None
    try:
        data = (repo_root / meta.content_path).read_bytes()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None
    if content_hash_for(data) != meta.content_hash:
        return None
    return data


async def resolve_source_cached(
    *,
    loader: SourceLoader,
    ref: SourceRefSpec,
    source_id: str,
    schedule_id: str,
    run_id: str,
    conn: sqlite3.Connection,
    conn_factory: Callable[[], sqlite3.Connection],
    clock: Callable[[], datetime],
    as_of_datetime: Optional[datetime],
    audit: AuditPolicy,
    repo_root: Path = _REPO_ROOT,
) -> CacheResolution:
    """Resolve a source through the per-source cache +
    fallback. See module docstring for the hard invariant.

    Order:
    1. Cache hit — a materialised snapshot newer than
       ``cache_ttl_seconds`` whose body verifies → serve
       it, skip the loader entirely.
    2. Else invoke the loader.
       - success → write a new snapshot, serve it (FRESH).
       - raises a NON-fallback ``SourceError`` (auth /
         security / policy / parse) → re-raise UNTOUCHED.
         No cache read, no fallback, no cache write.
       - raises ``SourceFetchError`` (the ONLY
         fallback-eligible class) → apply
         ``ref.cache.fallback_policy``.
    """
    now = clock()
    cache = ref.cache

    # ---- 1. cache hit (cross-fire TTL no-probe window) ----
    # SECURITY OPT-OUT (plan §1.4 / §9c): cache_ttl_seconds
    # == 0 (or negative) ⇒ the cross-fire cache-hit path is
    # SKIPPED unconditionally so the loader is probed EVERY
    # fire and a live SourceAuthError / SourceSecurityError
    # surfaces every time, never masked by a within-TTL hit.
    # A POSITIVE TTL is a deliberate no-probe window: within
    # it the captured snapshot IS the source and the loader
    # is not invoked — there is no live auth to mask (the
    # §3.5 non-fallback invariant governs the post-fetch
    # path only).
    if cache.cache_ttl_seconds > 0:
        newest = _newest_materialised_snapshot(
            conn, schedule_id=schedule_id, source_id=source_id
        )
        if newest is not None:
            age = now - newest.fetched_at
            if timedelta(0) <= age <= timedelta(
                seconds=cache.cache_ttl_seconds
            ):
                body = _read_verified_body(repo_root, newest)
                if body is not None:
                    return CacheResolution(
                        provenance=CacheProvenance.CACHE_HIT,
                        content_bytes=body,
                        content_hash=newest.content_hash,
                        snapshot=newest,
                    )
                # tampered / missing → fall through to the
                # loader; never serve an unverified body.

    # ---- 2. fetch ----
    loader_args: dict[str, Any] = {
        **ref.args,
        "source_id": source_id,
    }
    try:
        result = await loader.load(
            args=loader_args,
            as_of_datetime=as_of_datetime,
            clock=clock,
        )
    except SourceError as exc:
        if not exc.fallback_eligible:
            # auth / security / policy / parse — propagate
            # untouched. Cache is NEVER read, NEVER written
            # (a failed auth must not poison the cache),
            # fallback is NEVER consulted.
            raise
        # SourceFetchError → fallback_policy.
        return _apply_fallback(
            exc=exc,
            ref=ref,
            schedule_id=schedule_id,
            source_id=source_id,
            conn=conn,
            now=now,
            repo_root=repo_root,
        )

    # ---- success: write the new snapshot, serve it ----
    write = write_snapshot(
        conn,
        result=result,
        schedule_id=schedule_id,
        run_id=run_id,
        audit=audit,
        repo_root=repo_root,
    )
    return CacheResolution(
        provenance=CacheProvenance.FRESH,
        content_bytes=result.content_bytes,
        content_hash=result.content_hash,
        snapshot=write.row,
        fresh_result=result,
        fresh_write=write,
    )


def _apply_fallback(
    *,
    exc: SourceFetchError,
    ref: SourceRefSpec,
    schedule_id: str,
    source_id: str,
    conn: sqlite3.Connection,
    now: datetime,
    repo_root: Path,
) -> CacheResolution:
    """``SourceFetchError`` only — apply
    ``ref.cache.fallback_policy``. Never reached for a
    non-fallback error."""
    policy = ref.cache.fallback_policy

    if policy == SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT:
        newest = _newest_materialised_snapshot(
            conn, schedule_id=schedule_id, source_id=source_id
        )
        if newest is not None:
            age = now - newest.fetched_at
            if timedelta(0) <= age <= timedelta(
                seconds=ref.cache.stale_max_age_seconds
            ):
                body = _read_verified_body(repo_root, newest)
                if body is not None:
                    return CacheResolution(
                        provenance=CacheProvenance.FALLBACK_LAST_GOOD,
                        content_bytes=body,
                        content_hash=newest.content_hash,
                        snapshot=newest,
                    )
        # No fresh-enough verified last-good → the fetch
        # failure stands (slice-8 resolver → SOURCE_FAILED).
        raise exc

    if policy == SourceFallbackPolicy.ALERT_AND_USE_DEFAULT:
        # SourceRefSpec's validator guarantees
        # explicit_default is present for this policy.
        return CacheResolution(
            provenance=CacheProvenance.FALLBACK_DEFAULT,
            default_value=ref.explicit_default,
        )

    # ALERT_AND_SKIP → don't serve anything; the fetch
    # failure propagates (resolver → SOURCE_FAILED).
    raise exc


__all__ = [
    "CacheProvenance",
    "CacheResolution",
    "resolve_source_cached",
]
