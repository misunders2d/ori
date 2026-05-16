"""Phase 10 slice 8 — source resolver orchestrator.

Per ``docs/PHASE_10_PLAN.md`` §3.3 / §3.5 / §1.5 / §5.
EXACTLY ONE event per resolve on EVERY path (incl. each
error class + the internal-error guard); typed-error
routing; live-change drift; never raises into the caller.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.v2.descriptors.source import SourceDescriptor
from app.v2.enums import (
    EventKind,
    LiveChangePolicy,
    SelectionMethod,
    SourceFallbackPolicy,
)
from app.v2.migrations import runner
from app.v2.models.common import AuditPolicy, LiveSourceCachePolicy
from app.v2.models.source_ref import SourceRefSpec
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
from app.v2.sources.registry import SourceLoaderRegistry
from app.v2.sources.resolver import (
    ResolveStatus,
    resolve_source,
)
from app.v2.sources.snapshot_writer import write_snapshot
from app.v2.storage.source_snapshots import get_snapshot
from app.v2.tool_tags import ToolCapabilityTag


_T0 = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


def _clock():
    return _T0


def _eid_factory():
    n = {"i": 0}

    def f() -> str:
        n["i"] += 1
        return f"evt-{n['i']:08d}-1111-1111-1111-111111111111"

    return f


class _StubLoader:
    descriptor = SourceDescriptor(
        id="source_stub",
        description="resolver stub loader",
        tags={ToolCapabilityTag.READ_EXTERNAL},
        supports_versioning=False,
        supported_selection_methods=[SelectionMethod.CONTENT_HASH],
    )

    def __init__(self, *, text=None, raises=None):
        self.calls = 0
        self._text = text
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

    def factory():
        c = sqlite3.connect(str(path), isolation_level=None)
        c.execute("PRAGMA foreign_keys=ON")
        return c

    return factory


def _seed_schedule(conn, sid="sched_a"):
    conn.execute(
        "INSERT INTO schedules (id, owner, description, "
        "trigger_json, delivery_json, failure_json, "
        "audit_json, status, authored_at, hash) VALUES "
        "(?,?,?,?,?,?,?,?,?,?)",
        (sid, "{}", "t", "{}", "{}", "{}", "{}", "active",
         _T0.isoformat(), f"h-{sid}"),
    )


def _seed_run(conn, rid, sid="sched_a", at=_T0):
    conn.execute(
        "INSERT INTO runs (id, schedule_id, fire_reason, "
        "due_at, status, attempt, root_run_id) VALUES "
        "(?,?,?,?,?,?,?)",
        (rid, sid, "scheduled", at.isoformat(), "pending",
         1, rid),
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


def _ref(*, loader="source_stub", live=LiveChangePolicy.ALLOW,
         ttl=0, fallback=SourceFallbackPolicy.ALERT_AND_SKIP,
         default=None):
    kw = dict(
        loader=loader,
        args={},
        cache=LiveSourceCachePolicy(
            cache_ttl_seconds=ttl,
            stale_max_age_seconds=3600,
            fallback_policy=fallback,
        ),
        live_change_policy=live,
    )
    if fallback == SourceFallbackPolicy.ALERT_AND_USE_DEFAULT:
        kw["explicit_default"] = (
            default if default is not None else {"d": 1}
        )
    return SourceRefSpec(**kw)


def _loaders(stub):
    reg = SourceLoaderRegistry()
    reg.register(stub)
    return reg


def _events(factory, run_id):
    conn = factory()
    try:
        return conn.execute(
            "SELECT kind FROM events WHERE run_id = ? "
            "ORDER BY rowid",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()


async def _resolve(factory, stub, ref, *, run_id="r1",
                    tmp_path, eid=None):
    conn = factory()
    try:
        return await resolve_source(
            ref=ref,
            source_id="in1",
            schedule_id="sched_a",
            run_id=run_id,
            conn=conn,
            conn_factory=factory,
            clock=_clock,
            event_id_factory=eid or _eid_factory(),
            as_of_datetime=None,
            audit=_audit(),
            loaders=_loaders(stub),
            repo_root=tmp_path,
        )
    finally:
        conn.close()


def _seed_prior_snapshot(factory, tmp_path, *, run_id, text, at=_T0):
    conn = factory()
    try:
        _seed_run(conn, run_id, at=at)
        cb = canonical_bytes("text", text)
        res = SourceResult(
            content=text,
            content_bytes=cb,
            source_kind="source_stub",
            source_id="in1",
            fetched_at=at,
            content_hash=content_hash_for(cb),
            item_count=1,
            source_version=None,
            selection_method=SelectionMethod.CONTENT_HASH,
        )
        write_snapshot(
            conn, result=res, schedule_id="sched_a",
            run_id=run_id, audit=_audit(), repo_root=tmp_path,
        )
    finally:
        conn.close()


# ===========================================================================
# Success paths — exactly one SOURCE_RESOLVED
# ===========================================================================


@pytest.mark.asyncio
async def test_fresh_resolved_exactly_one_event(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
    finally:
        conn.close()
    out = await _resolve(factory, _StubLoader(text="hello"),
                          _ref(), tmp_path=tmp_path)
    assert out.status is ResolveStatus.RESOLVED
    assert out.event_kind is EventKind.SOURCE_RESOLVED
    assert out.content_bytes == b"hello"
    evs = _events(factory, "r1")
    assert evs == [("source_resolved",)]  # EXACTLY ONE


@pytest.mark.asyncio
async def test_cache_hit_resolved_one_event(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    _seed_prior_snapshot(factory, tmp_path, run_id="r0",
                         text="warm")
    _seed_run_conn = factory()
    try:
        _seed_run(_seed_run_conn, "r1")
    finally:
        _seed_run_conn.close()
    # ttl>0 + within window → CACHE_HIT (loader not called)
    stub = _StubLoader(raises=RuntimeError("must not call"))
    out = await _resolve(
        factory, stub, _ref(ttl=300), run_id="r1",
        tmp_path=tmp_path,
    )
    assert out.status is ResolveStatus.RESOLVED
    assert stub.calls == 0
    assert _events(factory, "r1") == [("source_resolved",)]


@pytest.mark.asyncio
async def test_fallback_default_resolved_one_event(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
    finally:
        conn.close()
    ref = _ref(
        fallback=SourceFallbackPolicy.ALERT_AND_USE_DEFAULT,
        default={"fallback": "v"},
    )
    out = await _resolve(
        factory, _StubLoader(raises=SourceFetchError("net")),
        ref, tmp_path=tmp_path,
    )
    assert out.status is ResolveStatus.RESOLVED
    assert out.default_value == {"fallback": "v"}
    assert _events(factory, "r1") == [("source_resolved",)]


@pytest.mark.asyncio
async def test_fallback_last_good_resolved_one_event(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    _seed_prior_snapshot(factory, tmp_path, run_id="r0",
                         text="lastgood")
    c = factory()
    try:
        _seed_run(c, "r1")
    finally:
        c.close()
    ref = _ref(
        ttl=0,  # skip cache-hit; force loader → fetch-fail
        fallback=SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT,
    )
    out = await _resolve(
        factory, _StubLoader(raises=SourceFetchError("net")),
        ref, tmp_path=tmp_path,
    )
    assert out.status is ResolveStatus.RESOLVED
    assert out.content_bytes == b"lastgood"
    assert _events(factory, "r1") == [("source_resolved",)]


# ===========================================================================
# Failure paths — exactly one SOURCE_FAILED, never fallback
# for non-fallback classes
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "err",
    [
        SourceAuthError("a"),
        SourceSecurityError("s"),
        SourcePolicyError("p"),
        SourceParseError("x"),
    ],
)
async def test_non_fallback_error_one_source_failed(tmp_path, err):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    # warm cache present + use_last_good — must NOT be served
    _seed_prior_snapshot(factory, tmp_path, run_id="r0",
                         text="warm")
    c = factory()
    try:
        _seed_run(c, "r1")
    finally:
        c.close()
    ref = _ref(
        ttl=0,  # probe every fire
        fallback=SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT,
    )
    out = await _resolve(
        factory, _StubLoader(raises=err), ref, tmp_path=tmp_path,
    )
    assert out.status is ResolveStatus.FAILED
    assert out.fallback_eligible is False
    assert out.content_bytes is None
    assert _events(factory, "r1") == [("source_failed",)]


@pytest.mark.asyncio
async def test_fetch_fail_no_cache_one_source_failed(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
    finally:
        conn.close()
    out = await _resolve(
        factory, _StubLoader(raises=SourceFetchError("net")),
        _ref(fallback=SourceFallbackPolicy.ALERT_AND_SKIP),
        tmp_path=tmp_path,
    )
    assert out.status is ResolveStatus.FAILED
    assert out.fallback_eligible is True
    assert _events(factory, "r1") == [("source_failed",)]


@pytest.mark.asyncio
async def test_unknown_loader_one_source_failed(tmp_path):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
    finally:
        conn.close()
    # registry has a different loader; ref.loader unknown
    out = await _resolve(
        factory, _StubLoader(text="x"),
        _ref(loader="source_nope"), tmp_path=tmp_path,
    )
    assert out.status is ResolveStatus.FAILED
    assert out.failure_code == "unknown_source_loader"
    assert out.fallback_eligible is False
    assert _events(factory, "r1") == [("source_failed",)]


@pytest.mark.asyncio
async def test_internal_error_still_one_source_failed(tmp_path):
    """An UNEXPECTED exception still funnels to EXACTLY ONE
    SOURCE_FAILED (no zero-emit); resolver never raises."""
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
    finally:
        conn.close()

    class _Boom(_StubLoader):
        async def load(self, *, args, as_of_datetime, clock):
            raise ValueError("not a SourceError")  # unexpected

    out = await _resolve(factory, _Boom(), _ref(),
                          tmp_path=tmp_path)
    assert out.status is ResolveStatus.FAILED
    assert out.failure_code == "source_resolver_internal_error"
    assert _events(factory, "r1") == [("source_failed",)]


# ===========================================================================
# Live-change policy (drift only on FRESH vs prior)
# ===========================================================================


async def _resolve_with_prior(tmp_path, live):
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
    finally:
        conn.close()
    _seed_prior_snapshot(factory, tmp_path, run_id="r0",
                         text="OLD")
    c = factory()
    try:
        _seed_run(c, "r1", at=_T0)
    finally:
        c.close()
    out = await _resolve(
        factory, _StubLoader(text="NEW"),
        _ref(ttl=0, live=live), run_id="r1", tmp_path=tmp_path,
    )
    return out, factory


@pytest.mark.asyncio
async def test_allow_no_drift_even_when_changed(tmp_path):
    out, factory = await _resolve_with_prior(
        tmp_path, LiveChangePolicy.ALLOW
    )
    assert out.status is ResolveStatus.RESOLVED
    assert out.content_bytes == b"NEW"
    assert _events(factory, "r1") == [("source_resolved",)]


@pytest.mark.asyncio
async def test_alert_on_shape_change_drift_still_serves(tmp_path):
    out, factory = await _resolve_with_prior(
        tmp_path, LiveChangePolicy.ALERT_ON_SHAPE_CHANGE
    )
    assert out.status is ResolveStatus.DRIFT
    assert out.event_kind is EventKind.SOURCE_DRIFT_DETECTED
    assert out.content_bytes == b"NEW"  # still serves
    assert _events(factory, "r1") == [("source_drift_detected",)]


@pytest.mark.asyncio
async def test_require_reapprove_fails_no_content(tmp_path):
    out, factory = await _resolve_with_prior(
        tmp_path,
        LiveChangePolicy.REQUIRE_REAPPROVE_ON_SHAPE_CHANGE,
    )
    assert out.status is ResolveStatus.FAILED
    assert (
        out.failure_code == "source_shape_change_requires_reapprove"
    )
    assert out.content_bytes is None  # withheld
    assert _events(factory, "r1") == [("source_failed",)]
    # the FRESH snapshot row IS written (audit of the change)
    conn = factory()
    try:
        assert (
            get_snapshot(conn, run_id="r1", source_id="in1")
            is not None
        )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_first_fire_no_prior_no_drift(tmp_path):
    """No prior materialised snapshot → never a shape
    change → RESOLVED regardless of policy."""
    factory = _db(tmp_path)
    conn = factory()
    try:
        _seed_schedule(conn)
        _seed_run(conn, "r1")
    finally:
        conn.close()
    out = await _resolve(
        factory, _StubLoader(text="first"),
        _ref(
            ttl=0,
            live=LiveChangePolicy.REQUIRE_REAPPROVE_ON_SHAPE_CHANGE,
        ),
        tmp_path=tmp_path,
    )
    assert out.status is ResolveStatus.RESOLVED
    assert _events(factory, "r1") == [("source_resolved",)]
