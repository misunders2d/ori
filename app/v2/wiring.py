"""v2 scheduler — production wiring for the CoordinatorAgent
mount.

Phase 9 slice 8 per ``docs/PHASE_9_PLAN.md`` §3.8.

This module is the single seam where the
:class:`AuthoringToolset` (phase 7) meets the Coordinator
agent (v1). It builds the module-level DI singletons every
authoring tool / template-wrapper closure needs:

- ``DraftStore`` / ``HandshakeStore`` — file-backed persistence.
- ``conn_factory`` — opens a per-call connection against the
  v2 state DB (separate file from the APScheduler jobstore
  per round-1 reviewer Q9).
- ``cache_loader`` / ``cache_saver`` — Slack channels cache
  reader / writer. Returns ``None`` when the cache is
  absent so the closure surfaces ``cache_unavailable`` per
  plan §3.3.
- ``slack_client`` (channels-list client) — kept ``None``
  in phase 9; cache must be pre-populated. Future phases
  wire a real ``conversations.list`` client.
- ``owner`` / ``session_id`` — single-tenant phase-9
  defaults: owner reads from
  :data:`app.v2.runtime._owner_default.DEFAULT_AUTHORING_OWNER_ID`
  (slice 6); session_id is the static ``"system_authoring"``
  slug. Multi-tenant routing is out of scope for phase 9.

The exported :func:`build_authoring_toolset` is the only
public surface — the Coordinator imports it once at module
load and adds the result to its ``tools=[…]`` list. The
toolset is **additive**: the v1 ``ContractToolset`` stays
mounted alongside it per round-2 reviewer Q4.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
from typing import TYPE_CHECKING, Callable, Optional

from app.v2.authoring.drafts import DraftStore
from app.v2.authoring.handshake import HandshakeStore
from app.v2.authoring.templates import make_schedule_create_reminder
from app.v2.emit.slack_reminder import SlackProtocol
from app.v2.models.common import UserRef
from app.v2.registry_cache.loader import load_cache, save_cache
from app.v2.registry_cache.schemas import SlackChannelsCache
from app.v2.runtime._owner_default import DEFAULT_AUTHORING_OWNER_ID
from app.v2.toolsets.authoring import AuthoringToolset

if TYPE_CHECKING:  # pragma: no cover - type-only import
    from datetime import datetime


_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


_DEFAULT_STATE_DB_PATH = os.path.abspath("./data/scheduler-v2-state.db")
"""Same path ``run_bot.py`` boots the v2 runtime against
(``_V2_STATE_DB_PATH``). Keeping these in sync is intentional:
the authoring tools write to the same ``schedules`` /
``events`` tables the worker pool reads."""


_SYSTEM_AUTHORING_SESSION_ID = "system_authoring"
"""Static session-id slug for the bot's own authoring
workflow. Single-tenant phase-9 placeholder; multi-tenant
session routing lands in a later phase."""


# ---------------------------------------------------------------------------
# Production wirings
# ---------------------------------------------------------------------------


def _open_state_conn(db_path: str) -> sqlite3.Connection:
    """Open a per-call SQLite connection against the v2 state
    DB with the required pragmas. Mirrors ``app/v2/boot.py``
    so the authoring path uses the same connection contract
    the worker pool does."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _prod_conn_factory(db_path: str):
    """Return a closure that opens a fresh connection per call."""

    def _factory() -> sqlite3.Connection:
        return _open_state_conn(db_path)

    return _factory


def _prod_cache_loader(
    *, expected_owner_id: Optional[str]
):
    """Return a closure that reads the Slack channels cache
    or ``None`` if absent.

    Errors during read are logged + swallowed; the caller
    treats ``None`` as 'cache absent' which routes to
    ``cache_unavailable`` per plan §3.3."""

    def _loader() -> Optional[SlackChannelsCache]:
        try:
            cache_file = load_cache(
                "slack_channels",
                expected_owner_id=expected_owner_id,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "v2 slack channels cache load failed: %s", exc
            )
            return None
        if cache_file is None:
            return None
        payload = cache_file.payload
        if isinstance(payload, SlackChannelsCache):
            return payload
        return None

    return _loader


def _prod_cache_saver(*, expected_owner_id: Optional[str]):
    """Return a closure that writes a refreshed Slack channels
    cache to disk. Errors are logged + swallowed so a
    write-side failure does not propagate into the LLM-visible
    tool response."""

    def _saver(cache: SlackChannelsCache) -> None:
        try:
            save_cache(
                cache,
                expected_owner_id=expected_owner_id,
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "v2 slack channels cache save failed: %s", exc
            )

    return _saver


def _system_owner(owner_id: Optional[str]) -> Optional[UserRef]:
    """Build the bot's system-owner :class:`UserRef` from the
    env-derived owner id. Returns ``None`` when the env is
    unset so :func:`build_authoring_toolset` can let
    :class:`AuthoringToolset` raise its documented startup
    error (slice 6)."""
    if owner_id is None:
        return None
    return UserRef(
        platform="system",
        user_id=owner_id,
        display_name="Ori",
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_authoring_toolset(
    *,
    db_path: str = _DEFAULT_STATE_DB_PATH,
    expected_owner_id: Optional[str] = None,
    slack_client: Optional[SlackProtocol] = None,
    clock: Optional[Callable[[], "datetime"]] = None,
    event_id_factory: Optional[Callable[[], str]] = None,
    schedule_id_factory: Optional[Callable[[], str]] = None,
) -> AuthoringToolset:
    """Build the production :class:`AuthoringToolset` ready
    for mount on the CoordinatorAgent.

    Wires the ``schedule_create_reminder`` closure with every
    DI dependency bound. ``expected_owner_id`` defaults to
    :data:`DEFAULT_AUTHORING_OWNER_ID` (slice 6 env fallback);
    when None at construction the AuthoringToolset's own
    constructor raises ``RuntimeError`` with the documented
    startup message.

    The three production wirings (``clock`` /
    ``event_id_factory`` / ``schedule_id_factory``) default
    to ``None`` and are resolved from
    :mod:`app.v2.runtime._defaults` via a **lazy import
    inside this function body** -- never at module load.
    ``_defaults`` is the SOLE ``uuid`` / ``datetime.now``
    binding site; the phase-9 hard rule forbids any phase-9
    new module (this one included) from importing it at
    module load or calling ``uuid.uuid4`` directly
    (slice-8 reviewer 🔴, pinned by
    ``tests/v2/test_phase9_import_hygiene.py``).

    Args:
        db_path: SQLite state DB path. Defaults to the same
            path ``run_bot.py`` boots the v2 runtime against.
        expected_owner_id: Tenant id. Falls back to env via
            :data:`DEFAULT_AUTHORING_OWNER_ID` when None.
        slack_client: Optional Slack channels API client
            (Protocol-typed). Phase 9 keeps this ``None``;
            the cache must be pre-populated.
        clock / event_id_factory / schedule_id_factory:
            Production wirings. ``None`` → resolved lazily
            from :mod:`app.v2.runtime._defaults`. Tests pass
            deterministic stubs to override.

    Returns: A :class:`AuthoringToolset` with
        ``schedule_create_reminder`` bound to the production
        closure (or the stub when ``owner`` can't be
        resolved).
    """
    # Lazy import: phase-9 hard rule forbids a module-load
    # ``app.v2.runtime._defaults`` import in this phase-9
    # module (it would re-export the sole uuid/datetime.now
    # binding surface here). Resolve production wirings at
    # call time instead.
    from app.v2.runtime._defaults import (
        prod_clock,
        prod_event_id_factory,
        prod_schedule_id_factory,
    )

    if clock is None:
        clock = prod_clock
    if event_id_factory is None:
        event_id_factory = prod_event_id_factory
    if schedule_id_factory is None:
        schedule_id_factory = prod_schedule_id_factory

    resolved_owner_id = (
        expected_owner_id
        if expected_owner_id is not None
        else DEFAULT_AUTHORING_OWNER_ID
    )
    owner = _system_owner(resolved_owner_id)

    closure = None
    if owner is not None:
        closure = make_schedule_create_reminder(
            store=DraftStore(),
            handshake_store=HandshakeStore(),
            conn_factory=_prod_conn_factory(db_path),
            cache_loader=_prod_cache_loader(
                expected_owner_id=resolved_owner_id
            ),
            cache_saver=_prod_cache_saver(
                expected_owner_id=resolved_owner_id
            ),
            slack_client=None,  # channels-list client, phase 9 deferred
            expected_owner_id=resolved_owner_id,
            clock=clock,
            event_id_factory=event_id_factory,
            schedule_id_factory=schedule_id_factory,
            owner=owner,
            session_id=_SYSTEM_AUTHORING_SESSION_ID,
        )

    # Pass the ALREADY-RESOLVED owner id to the toolset
    # constructor (not the raw kwarg). The toolset's own
    # slice-6 fallback resolves against
    # ``_owner_default.DEFAULT_AUTHORING_OWNER_ID``; if we
    # passed the raw ``None`` kwarg here it would re-resolve
    # independently and could disagree with the closure's
    # owner when this module's ``DEFAULT_AUTHORING_OWNER_ID``
    # was monkeypatched / differs. Resolving once + passing
    # the concrete value keeps the closure's ``owner`` and
    # the toolset's ``_expected_owner_id`` consistent. When
    # ``resolved_owner_id`` is None (no kwarg + no env) the
    # constructor raises RuntimeError per slice 6 — the
    # documented startup gate.
    return AuthoringToolset(
        expected_owner_id=resolved_owner_id,
        slack_client=slack_client,
        schedule_create_reminder=closure,
    )


__all__ = [
    "build_authoring_toolset",
]
