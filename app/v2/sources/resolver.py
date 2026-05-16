"""v2 scheduler — source resolver orchestrator.

Phase 10 slice 8 (FINAL build slice) per
``docs/PHASE_10_PLAN.md`` §3.3 / §3.5 / §1.5 + design
§5.3.3.

Ties the loader registry + per-source cache + snapshot
writer + fallback + live-change policy together and
returns **EXACTLY ONE** terminal :class:`ResolveOutcome`
per call — ``RESOLVED`` / ``DRIFT`` / ``FAILED`` — on
every path, including any exception, and NEVER raises into
the caller. It ATTEMPTS exactly one matching terminal
EventLedger row (``SOURCE_RESOLVED`` /
``SOURCE_DRIFT_DETECTED`` / ``SOURCE_FAILED``); that row is
**best-effort** — if the terminal emit ITSELF fails it is
logged and SWALLOWED, the outcome still returns with
``event_emitted=False`` / ``event_id=None``, and the
terminal status is NOT flipped by the emit failure. So: at
most one event row is ever persisted, exactly one emit is
attempted, no path double-emits, and no path returns zero
*outcomes* — a zero-*emit* (no row persisted) is a
possible, explicit, hardened outcome, not a contract
violation (see :func:`resolve_source`).

Routing (the §3.5 taxonomy, slice-5 semantics):
- loader resolution + cache + fallback delegate to
  :func:`app.v2.sources.cache.resolve_source_cached` —
  ONLY ``SourceFetchError`` is fallback-eligible; auth /
  security / policy / parse propagate and become
  ``SOURCE_FAILED`` (never cache / never fallback). The
  cache no-probe window (``cache_ttl_seconds > 0``) and
  the probe-every-fire opt-out (``== 0``) are honoured by
  the cache layer; the resolver does not re-implement
  them.
- Live-change policy (only on a FRESH fetch — a
  ``CACHE_HIT`` / fallback served no new content so there
  is nothing to compare): the "shape" proxy for phase 10
  is the content hash vs the immediately-prior
  materialised snapshot (``SourceSnapshotMetadata`` does
  not persist ``item_count``; ``content_hash`` is the
  available discriminator). ``allow`` → no drift event;
  ``alert_on_shape_change`` → ``SOURCE_DRIFT_DETECTED``
  and STILL serves; ``require_reapprove_on_shape_change``
  → ``SOURCE_FAILED`` and the content is NOT served
  (the FRESH snapshot row/file is still written — it is
  the audit evidence of the change the admin must
  re-approve; the resolver simply withholds it from the
  fire).

Build-the-layer: registered in the SOURCES layer but NOT
wired into the worker fire path — source-driven schedules
fire at step 11 (a later phase).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
import sqlite3

from pydantic.types import JsonValue

from app.v2.enums import EventKind, LiveChangePolicy
from app.v2.models.common import AuditPolicy
from app.v2.models.event import Event
from app.v2.models.source_ref import SourceRefSpec
from app.v2.registry import UnknownSourceError
from app.v2.sources.cache import (
    CacheProvenance,
    CacheResolution,
    _newest_materialised_snapshot,
    resolve_source_cached,
)
from app.v2.sources.errors import SourceError
from app.v2.sources.registry import SOURCE_LOADERS, SourceLoaderRegistry
from app.v2.storage.events import append_event
from app.v2.storage.transactions import transaction

_logger = logging.getLogger(__name__)

# repo root = dir containing app/ (app/v2/sources/resolver.py)
_REPO_ROOT = Path(__file__).resolve().parents[3]


class ResolveStatus(str, Enum):
    RESOLVED = "resolved"
    DRIFT = "drift"
    FAILED = "failed"


@dataclass(frozen=True)
class ResolveOutcome:
    """The single typed result of a resolve. Carries the
    served body for RESOLVED / DRIFT; FAILED serves
    nothing.

    ``event_emitted`` is True iff the terminal event row
    was actually persisted; ``event_id`` is its id (None
    when the terminal emit ITSELF failed — see the
    hardened contract in :func:`resolve_source`)."""

    status: ResolveStatus
    event_kind: EventKind
    event_id: Optional[str]
    event_emitted: bool
    source_id: str
    provenance: Optional[CacheProvenance] = None
    content_bytes: Optional[bytes] = None
    content_hash: Optional[str] = None
    default_value: Optional[JsonValue] = None
    failure_code: Optional[str] = None
    fallback_eligible: Optional[bool] = None


async def resolve_source(
    *,
    ref: SourceRefSpec,
    source_id: str,
    schedule_id: str,
    run_id: str,
    conn: sqlite3.Connection,
    conn_factory: Callable[[], sqlite3.Connection],
    clock: Callable[[], datetime],
    event_id_factory: Callable[[], str],
    as_of_datetime: Optional[datetime] = None,
    audit: AuditPolicy,
    loaders: SourceLoaderRegistry = SOURCE_LOADERS,
    repo_root: Path = _REPO_ROOT,
) -> ResolveOutcome:
    """Resolve ``ref`` for one fire and return a single
    well-defined :class:`ResolveOutcome`.

    **Hardened terminal-emit contract (codex slice-8 🔴).**
    The resolver attempts EXACTLY ONE terminal EventLedger
    emit (``SOURCE_RESOLVED`` / ``SOURCE_DRIFT_DETECTED`` /
    ``SOURCE_FAILED``) and NEVER raises into the caller —
    on ANY path, including when the terminal emit ITSELF
    fails (``append_event`` integrity / DB error, a
    duplicate event id, a raising ``event_id_factory``,
    a bad clock, …). A terminal-emit failure is
    **best-effort**: it is logged and SWALLOWED (never
    raised), and the resolver still returns the single
    well-defined terminal outcome with
    ``event_emitted=False`` / ``event_id=None``. So: at
    most one event row is ever persisted, exactly one emit
    is attempted, and there is NO path that double-emits,
    flips the terminal status because the emit failed, or
    propagates the append/DB exception.
    """

    def _emit(
        kind: EventKind, payload: dict[str, Any]
    ) -> Optional[str]:
        """Best-effort terminal emit. Returns the event id
        on success, or None if the emit ITSELF failed
        (logged, never raised). Catches BaseException so
        even a ``KeyboardInterrupt``-class failure during
        the append cannot break the never-raise / single-
        outcome guarantee."""
        try:
            eid = event_id_factory()
            event = Event(
                id=eid,
                run_id=run_id,
                schedule_id=schedule_id,
                ts=clock(),
                kind=kind,
                payload=payload,
            )
            with transaction(conn):
                append_event(conn, event)
            return eid
        except BaseException:  # noqa: BLE001 - terminal-emit guard
            _logger.exception(
                "source resolver: terminal %s emit FAILED "
                "(best-effort — swallowed; no event row "
                "persisted) source_id=%s schedule_id=%s "
                "run_id=%s",
                kind.value,
                source_id,
                schedule_id,
                run_id,
            )
            return None

    def _failed(
        code: str, *, fallback_eligible: Optional[bool] = None
    ) -> ResolveOutcome:
        eid = _emit(
            EventKind.SOURCE_FAILED,
            {
                "source_id": source_id,
                "code": code,
                "fallback_eligible": bool(fallback_eligible),
            },
        )
        return ResolveOutcome(
            status=ResolveStatus.FAILED,
            event_kind=EventKind.SOURCE_FAILED,
            event_id=eid,
            event_emitted=eid is not None,
            source_id=source_id,
            failure_code=code,
            fallback_eligible=fallback_eligible,
        )

    try:
        # Loader dispatch (unknown id is a config failure —
        # non-fallback).
        try:
            loader = loaders.require(ref.loader)
        except UnknownSourceError:
            return _failed(
                "unknown_source_loader", fallback_eligible=False
            )

        # Snapshot the prior materialised hash BEFORE the
        # (possible) FRESH write, for the live-change check.
        prior = _newest_materialised_snapshot(
            conn, schedule_id=schedule_id, source_id=source_id
        )
        prior_hash = prior.content_hash if prior is not None else None

        try:
            res: CacheResolution = await resolve_source_cached(
                loader=loader,
                ref=ref,
                source_id=source_id,
                schedule_id=schedule_id,
                run_id=run_id,
                conn=conn,
                conn_factory=conn_factory,
                clock=clock,
                as_of_datetime=as_of_datetime,
                audit=audit,
                repo_root=repo_root,
            )
        except SourceError as exc:
            return _failed(
                exc.payload_code,
                fallback_eligible=exc.fallback_eligible,
            )

        # FALLBACK_DEFAULT serves the explicit default (no
        # body / hash); never a drift candidate.
        if res.provenance is CacheProvenance.FALLBACK_DEFAULT:
            eid = _emit(
                EventKind.SOURCE_RESOLVED,
                {
                    "source_id": source_id,
                    "provenance": res.provenance.value,
                },
            )
            return ResolveOutcome(
                status=ResolveStatus.RESOLVED,
                event_kind=EventKind.SOURCE_RESOLVED,
                event_id=eid,
                event_emitted=eid is not None,
                source_id=source_id,
                provenance=res.provenance,
                default_value=res.default_value,
            )

        # Live-change only applies to a FRESH fetch — a
        # CACHE_HIT / FALLBACK_LAST_GOOD served no new
        # content to compare.
        shape_changed = (
            res.provenance is CacheProvenance.FRESH
            and prior_hash is not None
            and res.content_hash != prior_hash
        )

        if shape_changed and (
            ref.live_change_policy
            is LiveChangePolicy.REQUIRE_REAPPROVE_ON_SHAPE_CHANGE
        ):
            # The FRESH snapshot row/file IS written (audit
            # of the change to re-approve); the fire is
            # FAILED and the content is withheld.
            return _failed(
                "source_shape_change_requires_reapprove",
                fallback_eligible=False,
            )

        if shape_changed and (
            ref.live_change_policy
            is LiveChangePolicy.ALERT_ON_SHAPE_CHANGE
        ):
            eid = _emit(
                EventKind.SOURCE_DRIFT_DETECTED,
                {
                    "source_id": source_id,
                    "provenance": res.provenance.value,
                    "content_hash": res.content_hash,
                    "prior_content_hash": prior_hash,
                    "change": "content_hash",
                },
            )
            return ResolveOutcome(
                status=ResolveStatus.DRIFT,
                event_kind=EventKind.SOURCE_DRIFT_DETECTED,
                event_id=eid,
                event_emitted=eid is not None,
                source_id=source_id,
                provenance=res.provenance,
                content_bytes=res.content_bytes,
                content_hash=res.content_hash,
            )

        # allow (or no shape change) → resolved.
        eid = _emit(
            EventKind.SOURCE_RESOLVED,
            {
                "source_id": source_id,
                "provenance": res.provenance.value,
                "content_hash": res.content_hash,
            },
        )
        return ResolveOutcome(
            status=ResolveStatus.RESOLVED,
            event_kind=EventKind.SOURCE_RESOLVED,
            event_id=eid,
            event_emitted=eid is not None,
            source_id=source_id,
            provenance=res.provenance,
            content_bytes=res.content_bytes,
            content_hash=res.content_hash,
        )

    except BaseException:  # noqa: BLE001 - terminal guard
        # Any unexpected failure BEFORE the terminal emit
        # (loader dispatch / prior-snapshot read / cache
        # logic) → exactly ONE best-effort SOURCE_FAILED
        # and return. ``_failed`` / ``_emit`` are themselves
        # guaranteed non-raising (the terminal-emit guard),
        # so this path cannot double-emit, flip status on
        # emit failure, or propagate — the resolver NEVER
        # raises into the caller.
        _logger.exception(
            "source resolver internal error for source_id=%s "
            "schedule_id=%s run_id=%s",
            source_id,
            schedule_id,
            run_id,
        )
        return _failed(
            "source_resolver_internal_error",
            fallback_eligible=False,
        )


__all__ = [
    "ResolveStatus",
    "ResolveOutcome",
    "resolve_source",
]
