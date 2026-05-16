"""Phase 10 slice 5 — per-source cache + fallback.

Per ``docs/PHASE_10_PLAN.md`` §1.4 / §3.3 / §3.5 / §5.
The error-class × cache(hit/miss) × fallback-policy
matrix, with the HARD invariant pinned explicitly: ONLY
SourceFetchError is fallback-eligible; auth / security /
policy / parse NEVER consult cache or fallback and NEVER
write the cache.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.v2.descriptors.source import SourceDescriptor
from app.v2.enums import (
    LiveChangePolicy,
    SelectionMethod,
    SourceFallbackPolicy,
)
from app.v2.migrations import runner
from app.v2.models.common import AuditPolicy, LiveSourceCachePolicy
from app.v2.models.source_ref import SourceRefSpec
from app.v2.sources.cache import (
    CacheProvenance,
    resolve_source_cached,
)
from app.v2.sources.contract import (
    SourceResult,
    canonical_bytes,
    content_hash_for,
)
from app.v2.sources.errors import (
    SourceAuthError,
    SourceFetchError,
    SourceParseError,
    SourcePolicyError,
    SourceSecurityError,
)
from app.v2.sources.snapshot_writer import write_snapshot
from app.v2.storage.source_snapshots import get_snapshot
from app.v2.tool_tags import ToolCapabilityTag


_T0 = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)
_NON_FALLBACK = [
    SourceAuthError,
    SourceSecurityError,
    SourcePolicyError,
    SourceParseError,
]


class _Clock:
    def __init__(self, t: datetime):
        self._t = t

    def __call__(self) -> datetime:
        return self._t

    def set(self, t: datetime):
        self._t = t


class _StubLoader:
    descriptor = SourceDescriptor(
        id="source_stub",
        description="stub loader for cache tests",
        tags={ToolCapabilityTag.READ_EXTERNAL},
        supports_versioning=False,
        supported_selection_methods=[SelectionMethod.CONTENT_HASH],
    )

    def __init__(self, *, result_text=None, raises=None):
        self.calls = 0
        self._text = result_text
        self._raises = raises

    async def load(self, *, args, as_of_datetime, clock):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        cb = canonical_bytes("text", self._text)
        return SourceResult(
            content=self._text,
            content_bytes=cb,
            source_kind="source_stub",
            source_id=args["source_id"],
            fetched_at=clock(),
            content_hash=content_hash_for(cb),
            item_count=1,
            source_version=None,
            selection_method=SelectionMethod.CONTENT_HASH,
        )


def _db(tmp_path: Path):
    path = tmp_path / "scheduler.db"
    init = sqlite3.connect(str(path))
    runner.apply_pending(init)
    init.close()

    def factory() -> sqlite3.Connection:
        # isolation_level=None = autocommit driver (the
        # production v2 pattern): bare seed INSERTs persist
        # immediately so a fresh conn sees them; transaction()
        # in write_snapshot still drives explicit BEGIN/COMMIT.
        c = sqlite3.connect(str(path), isolation_level=None)
        c.execute("PRAGMA foreign_keys=ON")
        return c

    return factory


def _seed_schedule(conn, schedule_id="sched_a"):
    conn.execute(
        "INSERT INTO schedules (id, owner, description, "
        "trigger_json, delivery_json, failure_json, "
        "audit_json, status, authored_at, hash) VALUES "
        "(?,?,?,?,?,?,?,?,?,?)",
        (schedule_id, "{}", "t", "{}", "{}", "{}", "{}",
         "active", _T0.isoformat(), f"h-{schedule_id}"),
    )


def _seed_run(conn, run_id, at, schedule_id="sched_a"):
    conn.execute(
        "INSERT INTO runs (id, schedule_id, fire_reason, "
        "due_at, status, attempt, root_run_id) VALUES "
        "(?,?,?,?,?,?,?)",
        (run_id, schedule_id, "scheduled", at.isoformat(),
         "pending", 1, run_id),
    )


def _audit(**kw):
    base = dict(
        keep_last_n_snapshots=30,
        dedup_by_content_hash=False,
        redact_fields=[],
        max_snapshot_bytes=1_000_000,
    )
    base.update(kw)
    return AuditPolicy(**base)


def _ref(
    *,
    fallback=SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT,
    ttl=300,
    stale_max=3600,
    default=None,
):
    kw = dict(
        loader="source_stub",
        args={},
        cache=LiveSourceCachePolicy(
            cache_ttl_seconds=ttl,
            stale_max_age_seconds=stale_max,
            fallback_policy=fallback,
        ),
        live_change_policy=LiveChangePolicy.ALLOW,
    )
    if fallback == SourceFallbackPolicy.ALERT_AND_USE_DEFAULT:
        kw["explicit_default"] = (
            default if default is not None else {"d": "fallback"}
        )
    return SourceRefSpec(**kw)


def _seed_cached(factory, tmp_path, *, run_id, text, fetched_at):
    """Materialise a warm cache entry (a real snapshot +
    .bin) at ``fetched_at``."""
    conn = factory()
    try:
        _seed_run(conn, run_id, fetched_at)
        cb = canonical_bytes("text", text)
        res = SourceResult(
            content=text,
            content_bytes=cb,
            source_kind="source_stub",
            source_id="in1",
            fetched_at=fetched_at,
            content_hash=content_hash_for(cb),
            item_count=1,
            source_version=None,
            selection_method=SelectionMethod.CONTENT_HASH,
        )
        out = write_snapshot(
            conn, result=res, schedule_id="sched_a",
            run_id=run_id, audit=_audit(), repo_root=tmp_path,
        )
    finally:
        conn.close()
    return out


async def _resolve(loader, ref, factory, clock, tmp_path, *, run_id="rN"):
    conn = factory()
    try:
        return await resolve_source_cached(
            loader=loader,
            ref=ref,
            source_id="in1",
            schedule_id="sched_a",
            run_id=run_id,
            conn=conn,
            conn_factory=factory,
            clock=clock,
            as_of_datetime=None,
            audit=_audit(),
            repo_root=tmp_path,
        )
    finally:
        conn.close()


def _seed_bare_run(factory, run_id, at):
    conn = factory()
    try:
        _seed_run(conn, run_id, at)
    finally:
        conn.close()


def _row_count(factory) -> int:
    conn = factory()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM source_snapshots"
        ).fetchone()[0]
    finally:
        conn.close()


# ===========================================================================
# Cache hit / miss → loader
# ===========================================================================


@pytest.mark.asyncio
async def test_cache_hit_skips_loader(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    _seed_cached(factory, tmp_path, run_id="r0", text="cached body",
                 fetched_at=_T0)

    clock = _Clock(_T0 + timedelta(seconds=100))  # < ttl 300
    loader = _StubLoader(raises=RuntimeError("must not be called"))
    res = await _resolve(loader, _ref(), factory, clock, tmp_path)

    assert res.provenance is CacheProvenance.CACHE_HIT
    assert res.content_bytes == b"cached body"
    assert loader.calls == 0


@pytest.mark.asyncio
async def test_cache_stale_beyond_ttl_calls_loader_fresh(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    _seed_cached(factory, tmp_path, run_id="r0", text="old",
                 fetched_at=_T0)
    _seed_bare_run(factory, "r1", _T0 + timedelta(seconds=999))

    clock = _Clock(_T0 + timedelta(seconds=999))  # > ttl 300
    loader = _StubLoader(result_text="fresh body")
    res = await _resolve(loader, _ref(), factory, clock, tmp_path,
                         run_id="r1")
    assert res.provenance is CacheProvenance.FRESH
    assert res.content_bytes == b"fresh body"
    assert loader.calls == 1


@pytest.mark.asyncio
async def test_no_cache_loader_success_is_fresh(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1", _T0)
    finally:
        conn.close()
    clock = _Clock(_T0)
    loader = _StubLoader(result_text="hello")
    res = await _resolve(loader, _ref(), factory, clock, tmp_path,
                         run_id="r1")
    assert res.provenance is CacheProvenance.FRESH
    assert loader.calls == 1
    conn = factory()
    try:
        assert get_snapshot(conn, run_id="r1", source_id="in1") is not None
    finally:
        conn.close()


# ===========================================================================
# SourceFetchError → fallback_policy (the ONLY eligible class)
# ===========================================================================


@pytest.mark.asyncio
async def test_fetch_fail_use_last_good_serves(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    _seed_cached(factory, tmp_path, run_id="r0", text="last good",
                 fetched_at=_T0)
    clock = _Clock(_T0 + timedelta(seconds=999))  # stale for ttl, fresh for stale_max
    loader = _StubLoader(raises=SourceFetchError("net down"))
    res = await _resolve(loader, _ref(), factory, clock, tmp_path,
                         run_id="r1")
    assert res.provenance is CacheProvenance.FALLBACK_LAST_GOOD
    assert res.content_bytes == b"last good"


@pytest.mark.asyncio
async def test_fetch_fail_last_good_too_old_raises(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    _seed_cached(factory, tmp_path, run_id="r0", text="ancient",
                 fetched_at=_T0)
    clock = _Clock(_T0 + timedelta(seconds=99999))  # > stale_max 3600
    loader = _StubLoader(raises=SourceFetchError("net down"))
    with pytest.raises(SourceFetchError):
        await _resolve(loader, _ref(), factory, clock, tmp_path,
                       run_id="r1")


@pytest.mark.asyncio
async def test_fetch_fail_no_cache_raises(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1", _T0)
    finally:
        conn.close()
    loader = _StubLoader(raises=SourceFetchError("net down"))
    with pytest.raises(SourceFetchError):
        await _resolve(loader, _ref(), factory, _Clock(_T0), tmp_path,
                       run_id="r1")


@pytest.mark.asyncio
async def test_fetch_fail_alert_and_use_default(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1", _T0)
    finally:
        conn.close()
    ref = _ref(
        fallback=SourceFallbackPolicy.ALERT_AND_USE_DEFAULT,
        default={"fallback": "copy"},
    )
    loader = _StubLoader(raises=SourceFetchError("net down"))
    res = await _resolve(loader, ref, factory, _Clock(_T0), tmp_path,
                         run_id="r1")
    assert res.provenance is CacheProvenance.FALLBACK_DEFAULT
    assert res.default_value == {"fallback": "copy"}


@pytest.mark.asyncio
async def test_fetch_fail_alert_and_skip_raises(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    _seed_cached(factory, tmp_path, run_id="r0", text="warm",
                 fetched_at=_T0)
    ref = _ref(fallback=SourceFallbackPolicy.ALERT_AND_SKIP)
    loader = _StubLoader(raises=SourceFetchError("net down"))
    # warm cache present but skip policy → still raises (no serve)
    with pytest.raises(SourceFetchError):
        await _resolve(loader, ref, factory,
                       _Clock(_T0 + timedelta(seconds=999)),
                       tmp_path, run_id="r1")


# ===========================================================================
# HARD invariant — non-fallback classes NEVER serve cache /
# default, NEVER write cache (codex slice-5 crux)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("err_cls", _NON_FALLBACK)
@pytest.mark.parametrize(
    "fallback",
    [
        SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT,
        SourceFallbackPolicy.ALERT_AND_USE_DEFAULT,
        SourceFallbackPolicy.ALERT_AND_SKIP,
    ],
)
async def test_non_fallback_error_with_warm_cache_still_raises(
    tmp_path, err_cls, fallback
):
    """Warm last-good snapshot present + a non-fallback
    error + ANY fallback policy → the error propagates
    UNTOUCHED. Never CACHE_HIT-from-stale, never
    FALLBACK_*. The cache is neither served nor written."""
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    _seed_cached(factory, tmp_path, run_id="r0", text="warm secret",
                 fetched_at=_T0)
    rows_before = _row_count(factory)

    ref = _ref(fallback=fallback)
    loader = _StubLoader(raises=err_cls("boom"))
    # clock past ttl so the cache-hit path is NOT taken —
    # the only way to "serve" would be the (forbidden)
    # fallback path.
    clock = _Clock(_T0 + timedelta(seconds=999))
    with pytest.raises(err_cls) as ei:
        await _resolve(loader, ref, factory, clock, tmp_path,
                       run_id="r1")
    assert ei.value.fallback_eligible is False
    # cache NOT written / refreshed by the failed attempt.
    assert _row_count(factory) == rows_before


@pytest.mark.asyncio
async def test_same_warm_cache_fetch_serves_auth_does_not(tmp_path):
    """The paired pin: identical warm cache + use_last_good
    — a SourceFetchError SERVES it; a SourceAuthError still
    raises (never RESOLVED-from-cache)."""
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    _seed_cached(factory, tmp_path, run_id="r0", text="WARM",
                 fetched_at=_T0)
    clock = _Clock(_T0 + timedelta(seconds=999))
    ref = _ref()  # use_last_good

    served = await _resolve(
        _StubLoader(raises=SourceFetchError("net")),
        ref, factory, clock, tmp_path, run_id="r1",
    )
    assert served.provenance is CacheProvenance.FALLBACK_LAST_GOOD
    assert served.content_bytes == b"WARM"

    with pytest.raises(SourceAuthError):
        await _resolve(
            _StubLoader(raises=SourceAuthError("401")),
            ref, factory, clock, tmp_path, run_id="r2",
        )


@pytest.mark.asyncio
async def test_auth_failure_does_not_write_cache(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1", _T0)
    finally:
        conn.close()
    before = _row_count(factory)
    with pytest.raises(SourceAuthError):
        await _resolve(
            _StubLoader(raises=SourceAuthError("401")),
            _ref(), factory, _Clock(_T0), tmp_path, run_id="r1",
        )
    assert _row_count(factory) == before  # cache not poisoned


# ===========================================================================
# Tampered / missing cached body is never served
# ===========================================================================


@pytest.mark.asyncio
async def test_tampered_cache_body_not_served_falls_through(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    out = _seed_cached(factory, tmp_path, run_id="r0",
                       text="original", fetched_at=_T0)
    _seed_bare_run(factory, "r1", _T0 + timedelta(seconds=100))
    # tamper the .bin
    (tmp_path / out.content_path).write_bytes(b"TAMPERED")

    clock = _Clock(_T0 + timedelta(seconds=100))  # within ttl
    loader = _StubLoader(result_text="fresh after tamper")
    res = await _resolve(loader, _ref(), factory, clock, tmp_path,
                         run_id="r1")
    # cache-hit window matched but body failed verification →
    # NOT served; fell through to the loader.
    assert res.provenance is CacheProvenance.FRESH
    assert loader.calls == 1


@pytest.mark.asyncio
async def test_missing_cache_body_not_served_on_fallback(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    out = _seed_cached(factory, tmp_path, run_id="r0",
                       text="gone", fetched_at=_T0)
    (tmp_path / out.content_path).unlink()  # body missing

    clock = _Clock(_T0 + timedelta(seconds=999))
    loader = _StubLoader(raises=SourceFetchError("net"))
    with pytest.raises(SourceFetchError):
        # use_last_good, but the last-good body is gone →
        # unverifiable → NOT served → fetch error stands.
        await _resolve(loader, _ref(), factory, clock, tmp_path,
                       run_id="r1")
