"""Tests for ``app.v2.boot`` -- the top-level wrapper
``run_bot.py`` calls into.

Phase 9 slice 7 per ``docs/PHASE_9_PLAN.md`` §3.7.

The wrapper owns:
- the v2 state SQLite path (parent dir creation),
- migration apply on first boot (idempotent),
- per-call connection factory with foreign_keys=ON +
  isolation_level=None,
- delegation into
  :func:`app.v2.runtime.boot.boot_runtime` with
  ``autostart=False`` so the caller drives activation.

Pins:
- Fresh db → migrations applied → handle returned,
  ``_activated=False``, binding paused.
- Re-boot on the same db → migrations are a no-op (idempotent).
- Wrapper passes ``autostart=False`` through to
  :func:`boot_runtime` (handle's ``_activated`` is False on
  return).
- Parent directory auto-created when it does not exist.
- conn_factory closure produces ready connections (pragmas
  set; ``assert_connection_ready`` passes).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from app.v2.boot import boot_v2_runtime
from app.v2.runtime.boot import RuntimeHandle
from app.v2.storage.connection import assert_connection_ready


@pytest.mark.asyncio
async def test_fresh_db_applies_migrations_and_returns_unactivated_handle(
    tmp_path,
):
    db_path = str(tmp_path / "v2-state.db")
    assert not os.path.exists(db_path)

    handle = await boot_v2_runtime(db_path)
    try:
        assert isinstance(handle, RuntimeHandle)
        # Slice 7 contract: wrapper defers activation.
        assert handle._activated is False
        # Migrations applied → file exists + applied_migrations
        # row present (assert_connection_ready hits both).
        assert os.path.exists(db_path)
        conn = sqlite3.connect(db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            assert_connection_ready(conn)  # raises if not ready
        finally:
            conn.close()
    finally:
        # Shutdown the binding + worker pool so the test
        # event loop does not leak threads.
        from app.v2.runtime.boot import shutdown_runtime
        await shutdown_runtime(handle)


@pytest.mark.asyncio
async def test_reboot_on_same_db_is_idempotent(tmp_path):
    """A re-boot against the same DB must succeed and must
    not raise on migrations. ``apply_pending`` is
    idempotent; the wrapper must honour that."""
    db_path = str(tmp_path / "v2-state.db")
    h1 = await boot_v2_runtime(db_path)
    from app.v2.runtime.boot import shutdown_runtime
    await shutdown_runtime(h1)

    # Re-boot.
    h2 = await boot_v2_runtime(db_path)
    try:
        assert isinstance(h2, RuntimeHandle)
        assert h2._activated is False
    finally:
        await shutdown_runtime(h2)


@pytest.mark.asyncio
async def test_parent_directory_auto_created(tmp_path):
    """Wrapper creates the parent directory if it does not
    exist. First-deploy hosts that mount a fresh ``./data``
    volume rely on this."""
    nested = tmp_path / "nested" / "deeper"
    assert not nested.exists()
    db_path = str(nested / "v2-state.db")

    handle = await boot_v2_runtime(db_path)
    try:
        assert nested.exists()
        assert os.path.exists(db_path)
    finally:
        from app.v2.runtime.boot import shutdown_runtime
        await shutdown_runtime(handle)


@pytest.mark.asyncio
async def test_handle_workers_unstarted_until_activate(tmp_path):
    """Slice-7 carry-forward: the handle returned from the
    wrapper has ``_activated=False`` AND
    :meth:`RuntimeHandle.activate` can be called against it
    successfully (one-shot)."""
    db_path = str(tmp_path / "v2-state.db")
    handle = await boot_v2_runtime(db_path)
    try:
        assert handle._activated is False
        await handle.activate()
        assert handle._activated is True
    finally:
        from app.v2.runtime.boot import shutdown_runtime
        await shutdown_runtime(handle)


@pytest.mark.asyncio
async def test_slack_client_threaded_into_every_worker(tmp_path):
    """Slice-7 round-2 reviewer 🔴 fix: the wrapper MUST
    thread its ``slack_client`` kwarg through to every
    Worker. Without this, production reminders silently
    succeed without ``chat_postMessage`` firing."""

    class _StubSlackClient:
        async def chat_postMessage(self, *, channel, text):
            return {"ok": True}

    stub = _StubSlackClient()
    db_path = str(tmp_path / "v2-state.db")
    handle = await boot_v2_runtime(db_path, slack_client=stub)
    try:
        assert len(handle.workers) >= 1
        for w in handle.workers:
            # Worker stores the client on ``_slack_client``;
            # pinning the private attr is acceptable because
            # the emit-branch dispatch reads it as the worker
            # contract (slice 5 reviewer pin pattern).
            assert w._slack_client is stub
    finally:
        from app.v2.runtime.boot import shutdown_runtime
        await shutdown_runtime(handle)


@pytest.mark.asyncio
async def test_wrapper_without_slack_client_passes_none(tmp_path):
    """Backwards-compat: omitting the kwarg keeps
    ``_slack_client=None`` on every worker (phase-4 empty-
    body fallback)."""
    db_path = str(tmp_path / "v2-state.db")
    handle = await boot_v2_runtime(db_path)
    try:
        for w in handle.workers:
            assert w._slack_client is None
    finally:
        from app.v2.runtime.boot import shutdown_runtime
        await shutdown_runtime(handle)
