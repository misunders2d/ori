"""Phase 11 slice 4 — phase-10 resolver contract regression.

Per ``docs/PHASE_11_PLAN.md`` §0.1 / §5. Slice 4 adds ONE
additive field — ``ResolveOutcome.changed_vs_prior:
Optional[bool] = None`` — to the phase-10-FROZEN
``app/v2/sources/resolver.py`` and a pure helper
``_changed_vs_prior`` implementing the §3.3 all-provenance
table. This file pins that the field add did NOT disturb
the phase-10 contract:

(a) every pre-existing ``ResolveOutcome`` construction /
    consumer is byte-unaffected (additive, defaulted);
(b) ``resolve_source`` still returns EXACTLY ONE terminal
    outcome and writes EXACTLY ONE terminal event;
(c) ``resolve_source`` still NEVER raises into the caller
    (even on a forced internal fault);
(d) the §3.5 typed-error taxonomy + ``fallback_eligible``
    ClassVar are intact;
(e) ``_changed_vs_prior`` is correct for ALL provenances
    (the §3.3 table), computed race-free from a
    caller-supplied ``prior_hash`` (no DB access).

``cache.py`` is NOT touched; the worker does NOT consume
``changed_vs_prior`` until slice 6. The broad behavioural
regression is the pre-existing ``test_source_resolver.py``
suite (re-run unchanged in the full v2 run — it never
references ``changed_vs_prior``, so its green status is
itself proof the field is purely additive).
"""

from __future__ import annotations

import dataclasses
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
from app.v2.sources import resolver as resolver_mod
from app.v2.sources.cache import CacheProvenance
from app.v2.sources.contract import (
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
from app.v2.sources.registry import SourceLoaderRegistry
from app.v2.sources.resolver import (
    ResolveOutcome,
    ResolveStatus,
    _changed_vs_prior,
    resolve_source,
)
from app.v2.tool_tags import ToolCapabilityTag

_T0 = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


# ===========================================================================
# (a) additive — pre-existing construction / consumers byte-unaffected
# ===========================================================================

# The phase-10 ResolveOutcome field set + order, frozen
# BEFORE slice 4. changed_vs_prior must be APPENDED last
# with a None default — nothing above it may move.
_PHASE10_FIELDS = (
    "status",
    "event_kind",
    "event_id",
    "event_emitted",
    "source_id",
    "provenance",
    "content_bytes",
    "content_hash",
    "default_value",
    "failure_code",
    "fallback_eligible",
)


def test_changed_vs_prior_is_additive_and_last():
    names = [f.name for f in dataclasses.fields(ResolveOutcome)]
    # Pre-existing fields unchanged in name AND order.
    assert tuple(names[: len(_PHASE10_FIELDS)]) == _PHASE10_FIELDS
    # The new field is appended last.
    assert names[-1] == "changed_vs_prior"
    assert len(names) == len(_PHASE10_FIELDS) + 1
    # …and defaults to None.
    cvp = next(
        f
        for f in dataclasses.fields(ResolveOutcome)
        if f.name == "changed_vs_prior"
    )
    assert cvp.default is None


def test_prior_consumer_construction_unaffected():
    """A pre-slice-4 caller constructs ResolveOutcome with
    ONLY the historical fields → changed_vs_prior defaults
    None, no signature break."""
    out = ResolveOutcome(
        status=ResolveStatus.RESOLVED,
        event_kind=EventKind.SOURCE_RESOLVED,
        event_id="evt-1",
        event_emitted=True,
        source_id="src",
    )
    assert out.changed_vs_prior is None
    assert out.status is ResolveStatus.RESOLVED
    # Still frozen (phase-10 contract).
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.changed_vs_prior = True  # type: ignore[misc]


# ===========================================================================
# (e) _changed_vs_prior — exhaustive §3.3 all-provenance table
# ===========================================================================


@pytest.mark.parametrize(
    "provenance, prior_hash, content_hash, expected",
    [
        # FRESH, no prior → first fire always "changed".
        (CacheProvenance.FRESH, None, "sha256:aa", True),
        # FRESH, prior == content → unchanged.
        (CacheProvenance.FRESH, "sha256:aa", "sha256:aa", False),
        # FRESH, prior != content → changed.
        (CacheProvenance.FRESH, "sha256:aa", "sha256:bb", True),
        # CACHE_HIT → snapshot IS the source; no new content.
        (CacheProvenance.CACHE_HIT, None, "sha256:aa", False),
        (CacheProvenance.CACHE_HIT, "sha256:aa", "sha256:bb", False),
        # FALLBACK_LAST_GOOD → re-serving last-good.
        (
            CacheProvenance.FALLBACK_LAST_GOOD,
            "sha256:aa",
            "sha256:aa",
            False,
        ),
        # FALLBACK_DEFAULT → None (degraded default MUST
        # surface; skip_unchanged never suppresses it).
        (CacheProvenance.FALLBACK_DEFAULT, None, None, None),
        # Defensive: unknown / None provenance → None.
        (None, "sha256:aa", "sha256:bb", None),
    ],
)
def test_changed_vs_prior_table(
    provenance, prior_hash, content_hash, expected
):
    assert (
        _changed_vs_prior(provenance, prior_hash, content_hash)
        is expected
    )


def test_changed_vs_prior_is_pure_no_db_args():
    """Race-free by construction: the helper's only inputs
    are the provenance + the prior_hash (captured BEFORE
    the FRESH write) + the content_hash. It has NO conn /
    factory / snapshot parameter, so it CANNOT re-read the
    snapshot table (the §0.1 🔴 race class)."""
    import inspect

    params = list(
        inspect.signature(_changed_vs_prior).parameters
    )
    assert params == ["provenance", "prior_hash", "content_hash"]


# ===========================================================================
# (d) §3.5 typed-error taxonomy + fallback_eligible ClassVar intact
# ===========================================================================


def test_fallback_eligible_classvar_unchanged_after_field_add():
    # Base default + the SOLE eligible subclass.
    assert SourceError.fallback_eligible is False
    assert SourceFetchError.fallback_eligible is True
    # Every other subclass stays non-fallback.
    for cls in (
        SourceAuthError,
        SourceSecurityError,
        SourcePolicyError,
        SourceParseError,
    ):
        assert cls.fallback_eligible is False
        assert issubclass(cls, SourceError)


# ===========================================================================
# (b)/(c)/(e-wiring) — resolve_source still exactly-one /
# never-raise, AND it wires _changed_vs_prior onto RESOLVED
# ===========================================================================


class _StubLoader:
    descriptor = SourceDescriptor(
        id="source_stub",
        description="regression stub loader",
        tags={ToolCapabilityTag.READ_EXTERNAL},
        supports_versioning=False,
        supported_selection_methods=[SelectionMethod.CONTENT_HASH],
    )

    def __init__(self, *, text="hello"):
        self._text = text

    async def load(self, *, args, as_of_datetime, clock):
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


def _seed(factory):
    conn = factory()
    try:
        conn.execute(
            "INSERT INTO schedules (id, owner, description, "
            "trigger_json, delivery_json, failure_json, "
            "audit_json, status, authored_at, hash) VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            ("sched_a", "{}", "t", "{}", "{}", "{}", "{}",
             "active", _T0.isoformat(), "h-sched_a"),
        )
        conn.execute(
            "INSERT INTO runs (id, schedule_id, fire_reason, "
            "due_at, status, attempt, root_run_id) VALUES "
            "(?,?,?,?,?,?,?)",
            ("r1", "sched_a", "scheduled", _T0.isoformat(),
             "pending", 1, "r1"),
        )
    finally:
        conn.close()


def _ref():
    return SourceRefSpec(
        loader="source_stub",
        args={},
        cache=LiveSourceCachePolicy(
            cache_ttl_seconds=0,
            stale_max_age_seconds=3600,
            fallback_policy=SourceFallbackPolicy.ALERT_AND_SKIP,
        ),
        live_change_policy=LiveChangePolicy.ALLOW,
    )


def _eid():
    n = {"i": 0}

    def f() -> str:
        n["i"] += 1
        return f"evt-{n['i']:08d}-1111-1111-1111-111111111111"

    return f


def _loaders():
    reg = SourceLoaderRegistry()
    reg.register(_StubLoader())
    return reg


def _events(factory):
    conn = factory()
    try:
        return conn.execute(
            "SELECT kind FROM events WHERE run_id = 'r1' "
            "ORDER BY rowid"
        ).fetchall()
    finally:
        conn.close()


async def _resolve(factory, tmp_path, loaders=None):
    conn = factory()
    try:
        return await resolve_source(
            ref=_ref(),
            source_id="in1",
            schedule_id="sched_a",
            run_id="r1",
            conn=conn,
            conn_factory=factory,
            clock=lambda: _T0,
            event_id_factory=_eid(),
            as_of_datetime=None,
            audit=AuditPolicy(
                keep_last_n_snapshots=30,
                dedup_by_content_hash=False,
                redact_fields=[],
                max_snapshot_bytes=1_000_000,
            ),
            loaders=loaders if loaders is not None else _loaders(),
            repo_root=tmp_path,
        )
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_fresh_resolved_still_exactly_one_event_and_wires_cvp(
    tmp_path,
):
    """After the field add: a FRESH-no-prior resolve still
    returns exactly ONE terminal outcome + writes exactly
    ONE SOURCE_RESOLVED event, AND changed_vs_prior is
    wired True (FRESH, no prior)."""
    factory = _db(tmp_path)
    _seed(factory)

    out = await _resolve(factory, tmp_path)

    assert isinstance(out, ResolveOutcome)
    assert out.status is ResolveStatus.RESOLVED
    assert out.event_kind is EventKind.SOURCE_RESOLVED
    assert _events(factory) == [("source_resolved",)]  # EXACTLY ONE
    # The additive field is wired from _changed_vs_prior:
    # FRESH + no prior snapshot → True.
    assert out.changed_vs_prior is True


@pytest.mark.asyncio
async def test_resolver_never_raises_on_internal_fault(
    tmp_path, monkeypatch
):
    """Phase-10 never-raise contract intact after the field
    add: a forced internal fault inside resolve_source_cached
    (a NON-SourceError BaseException) does NOT propagate —
    the resolver returns a single FAILED outcome and writes
    exactly ONE SOURCE_FAILED terminal event."""
    factory = _db(tmp_path)
    _seed(factory)

    async def _boom(**_kw):
        raise RuntimeError("simulated internal resolver fault")

    monkeypatch.setattr(
        resolver_mod, "resolve_source_cached", _boom
    )

    # MUST NOT raise.
    out = await _resolve(factory, tmp_path)

    assert isinstance(out, ResolveOutcome)
    assert out.status is ResolveStatus.FAILED
    assert out.event_kind is EventKind.SOURCE_FAILED
    assert out.failure_code == "source_resolver_internal_error"
    assert _events(factory) == [("source_failed",)]  # EXACTLY ONE
    # FAILED carries no served content / no cvp.
    assert out.content_bytes is None
    assert out.changed_vs_prior is None
