"""Tests for ``app.v2.runtime.binding`` -- slice 2 (lifecycle).

Pins per ``docs/PHASE_5_PLAN.md`` section 3.2 + section 5.2.

Slice 2 owns:
- Construction + injection validation.
- Lifecycle (start / stop / pause / resume / is_paused) +
  the documented paused-start / resume flow.
- ``_fire_for`` exception-swallow skeleton.

Registration (``register`` / ``unregister`` / ``reregister``)
is slice 3+ -- not exercised here. The boot sequence
(``boot_runtime``) is slice 5.

Coverage:

Construction:
- All defaults succeed.
- Custom jobstore_url succeeds.
- Empty jobstore_url -> ValueError.
- Negative misfire_grace_time -> ValueError.
- Zero misfire_grace_time accepted (boundary).
- Non-callable wakeup_callable / conn_factory / clock /
  run_id_factory / event_id_factory -> TypeError naming the
  offending kwarg.

Lifecycle:
- ``is_paused()`` returns False before ``start()``.
- ``start()`` then ``is_paused()`` False.
- ``start(paused=True)`` then ``is_paused()`` True.
- ``start()`` twice -> RuntimeError.
- ``stop()`` before ``start()`` is a no-op.
- ``stop()`` after ``start()`` clears the started flag;
  ``is_paused()`` False after.
- Double ``stop()`` is a no-op.
- ``await stop()`` yields the event loop so APScheduler's
  deferred ``_shutdown`` task lands before returning -- pin
  via observing scheduler.state transitions through 0.
- ``pause()`` before ``start()`` -> RuntimeError.
- ``resume()`` before ``start()`` -> RuntimeError.
- ``pause()`` after running -> ``is_paused()`` True.
- ``resume()`` after pause -> ``is_paused()`` False.
- Double pause + double resume are idempotent.
- ``start(paused=True)`` then await ``resume()`` ->
  ``is_paused()`` False.

APScheduler callback ``_fire_for``:
- Happy path: invokes wakeup with the documented kwargs;
  passes the clock's return value as ``now``; opens + closes
  the conn via ``conn_factory``.
- ``_fire_for`` swallows ``Exception`` raised by the wakeup;
  logs via ``logger.exception``; does NOT re-raise. The
  conn from ``conn_factory`` is still closed.
- ``_fire_for`` propagates ``BaseException`` (synthesises
  ``CancelledError``) so APScheduler's shutdown path sees
  the cancel signal. The conn is still closed.

Hygiene smoke:
- Module imports apscheduler (allowed) but no I/O libs.
- Module does not import uuid.
- Module exposes no reasoning / emit / delegate / transfer
  / sub_agent / dispatch / invoke public callable.
- ``binding._fire_for`` source contains the
  ``except Exception`` line (not ``except BaseException``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Callable

import pytest

# Reach the binding MODULE via sys.modules. The package
# __init__.py re-exports SchedulerBinding under the name
# ``SchedulerBinding`` (not ``binding``) so there is no
# attribute-shadow on the submodule -- but using
# sys.modules keeps the pattern uniform with the wakeup
# hygiene tests where shadowing IS a concern.
import app.v2.runtime  # noqa: F401 -- trigger package import

binding_mod = sys.modules["app.v2.runtime.binding"]

from app.v2.runtime.binding import SchedulerBinding


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _jobstore_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'jobs.db'}"


def _make_binding(tmp_path, **overrides) -> SchedulerBinding:
    """Construct a binding with sensible test defaults.
    Overrides replace individual kwargs."""
    kwargs = dict(
        wakeup_callable=lambda conn, **kw: [],
        conn_factory=lambda: sqlite3.connect(":memory:"),
        clock=lambda: _NOW,
        run_id_factory=lambda: "run-x",
        event_id_factory=lambda: "evt-x",
        jobstore_url=_jobstore_url(tmp_path),
    )
    kwargs.update(overrides)
    return SchedulerBinding(**kwargs)


# ===========================================================================
# Construction
# ===========================================================================


def test_construction_with_defaults_succeeds(tmp_path):
    """All defaults except the two required positionals +
    a tmp jobstore url. Should succeed without error."""
    b = SchedulerBinding(
        wakeup_callable=lambda conn, **kw: [],
        conn_factory=lambda: sqlite3.connect(":memory:"),
        jobstore_url=_jobstore_url(tmp_path),
    )
    assert b.is_paused() is False


def test_construction_with_explicit_jobstore_url(tmp_path):
    b = _make_binding(tmp_path)
    assert b is not None


def test_construction_with_zero_misfire_grace_time(tmp_path):
    """Zero is the boundary. Accepted (no fires after 0 s past
    due, but the param is still well-formed)."""
    b = _make_binding(tmp_path, misfire_grace_time=0)
    assert b is not None


def test_empty_jobstore_url_rejected(tmp_path):
    with pytest.raises(ValueError, match="jobstore_url"):
        _make_binding(tmp_path, jobstore_url="")


def test_negative_misfire_grace_time_rejected(tmp_path):
    with pytest.raises(ValueError, match="misfire_grace_time"):
        _make_binding(tmp_path, misfire_grace_time=-1)


@pytest.mark.parametrize(
    "field",
    ["wakeup_callable", "conn_factory", "clock", "run_id_factory", "event_id_factory"],
)
def test_non_callable_injectable_rejected(tmp_path, field):
    kwargs = dict(
        wakeup_callable=lambda conn, **kw: [],
        conn_factory=lambda: sqlite3.connect(":memory:"),
        clock=lambda: _NOW,
        run_id_factory=lambda: "r",
        event_id_factory=lambda: "e",
        jobstore_url=_jobstore_url(tmp_path),
    )
    kwargs[field] = "not-callable"
    with pytest.raises(TypeError, match=field):
        SchedulerBinding(**kwargs)


# ===========================================================================
# Lifecycle -- start / stop
# ===========================================================================


def test_is_paused_returns_false_before_start(tmp_path):
    b = _make_binding(tmp_path)
    assert b.is_paused() is False


@pytest.mark.asyncio
async def test_start_then_is_not_paused(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    try:
        assert b.is_paused() is False
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_start_paused_then_is_paused(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        assert b.is_paused() is True
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_start_twice_raises(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await b.start()
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_stop_before_start_is_noop(tmp_path):
    b = _make_binding(tmp_path)
    # Should not raise; should not hang.
    await b.stop()
    # State unchanged.
    assert b.is_paused() is False


@pytest.mark.asyncio
async def test_stop_after_start_clears_state(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    await b.stop()
    assert b.is_paused() is False


@pytest.mark.asyncio
async def test_double_stop_is_noop(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    await b.stop()
    # Second stop is a no-op (no exception, no APScheduler
    # SchedulerNotRunningError leakage).
    await b.stop()


@pytest.mark.asyncio
async def test_stop_yields_for_deferred_shutdown(tmp_path):
    """AsyncIOScheduler.shutdown() schedules ``_shutdown``
    onto the event loop; the actual state transition lands
    on the next loop iteration. ``stop()`` must yield via
    ``asyncio.sleep(0)`` so the caller can observe the
    completed shutdown directly. Pin by re-starting
    immediately after stop() returns -- if the shutdown
    weren't complete, start() would race the deferred
    _shutdown and the scheduler state would be inconsistent.
    """
    b = _make_binding(tmp_path)
    await b.start()
    await b.stop()
    # Immediate re-start must succeed -- shutdown is fully
    # done by the time stop() returned.
    await b.start()
    try:
        assert b.is_paused() is False
    finally:
        await b.stop()


# ===========================================================================
# Lifecycle -- pause / resume
# ===========================================================================


@pytest.mark.asyncio
async def test_pause_before_start_raises(tmp_path):
    b = _make_binding(tmp_path)
    with pytest.raises(RuntimeError, match="not running"):
        await b.pause()


@pytest.mark.asyncio
async def test_resume_before_start_raises(tmp_path):
    b = _make_binding(tmp_path)
    with pytest.raises(RuntimeError, match="not running"):
        await b.resume()


@pytest.mark.asyncio
async def test_pause_after_start(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    try:
        await b.pause()
        assert b.is_paused() is True
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_resume_after_pause(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        assert b.is_paused() is True
        await b.resume()
        assert b.is_paused() is False
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_double_pause_is_idempotent(tmp_path):
    b = _make_binding(tmp_path)
    await b.start()
    try:
        await b.pause()
        await b.pause()
        assert b.is_paused() is True
    finally:
        await b.stop()


@pytest.mark.asyncio
async def test_double_resume_is_idempotent(tmp_path):
    b = _make_binding(tmp_path)
    await b.start(paused=True)
    try:
        await b.resume()
        await b.resume()
        assert b.is_paused() is False
    finally:
        await b.stop()


# ===========================================================================
# _fire_for skeleton
# ===========================================================================


class _ConnSpy:
    """Sentinel connection that records close() invocations."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_fire_for_invokes_wakeup_with_documented_kwargs(tmp_path):
    """The binding's APScheduler callback must call
    ``wakeup_callable`` with exactly ``conn``, ``schedule_id``,
    ``now``, ``run_id_factory``, ``event_id_factory``. ``now``
    comes from the injected clock."""
    seen: dict = {}
    spy_conn = _ConnSpy()

    def wakeup_spy(conn, **kwargs):
        seen["conn"] = conn
        seen["kwargs"] = kwargs
        return []

    fixed_run_id = lambda: "run-fixed"
    fixed_evt_id = lambda: "evt-fixed"
    b = SchedulerBinding(
        wakeup_callable=wakeup_spy,
        conn_factory=lambda: spy_conn,
        clock=lambda: _NOW,
        run_id_factory=fixed_run_id,
        event_id_factory=fixed_evt_id,
        jobstore_url=_jobstore_url(tmp_path),
    )

    b._fire_for("my_schedule")

    assert seen["conn"] is spy_conn
    assert seen["kwargs"] == {
        "schedule_id": "my_schedule",
        "now": _NOW,
        "run_id_factory": fixed_run_id,
        "event_id_factory": fixed_evt_id,
    }
    # Connection was closed even though wakeup succeeded.
    assert spy_conn.closed is True


def test_fire_for_swallows_exception_and_logs(tmp_path, caplog):
    """A wakeup that raises Exception must NOT propagate.
    The error is logged via ``logger.exception`` and the
    method returns None. Pin: the connection is closed even
    on the exception path (finally clause)."""
    spy_conn = _ConnSpy()

    def wakeup_raises(conn, **kwargs):
        raise RuntimeError("synthetic wakeup failure")

    b = SchedulerBinding(
        wakeup_callable=wakeup_raises,
        conn_factory=lambda: spy_conn,
        jobstore_url=_jobstore_url(tmp_path),
    )

    with caplog.at_level(logging.ERROR, logger=binding_mod.__name__):
        # Must NOT raise.
        result = b._fire_for("broken_schedule")

    assert result is None
    # Connection closed via finally.
    assert spy_conn.closed is True
    # logger.exception ran for the broken schedule.
    assert any(
        r.levelno == logging.ERROR and "broken_schedule" in r.getMessage()
        for r in caplog.records
    )


def test_fire_for_propagates_base_exception(tmp_path):
    """A wakeup that raises BaseException (e.g.
    CancelledError) MUST propagate out of ``_fire_for`` so
    APScheduler's shutdown path can act on the signal.
    Same contract as Worker._run_loop in phase 4."""
    spy_conn = _ConnSpy()

    def wakeup_cancels(conn, **kwargs):
        raise asyncio.CancelledError("synthetic cancel")

    b = SchedulerBinding(
        wakeup_callable=wakeup_cancels,
        conn_factory=lambda: spy_conn,
        jobstore_url=_jobstore_url(tmp_path),
    )

    with pytest.raises(asyncio.CancelledError):
        b._fire_for("any_schedule")
    # Connection still closed via finally even though
    # BaseException propagated.
    assert spy_conn.closed is True


def test_fire_for_closes_connection_after_factory_call_succeeds(tmp_path):
    """Pin the finally clause: the conn opened by
    conn_factory is closed regardless of the wakeup outcome.
    Already covered above; this is the explicit success
    path so future refactors can't accidentally leave it
    open on the happy path."""
    spy_conn = _ConnSpy()
    b = SchedulerBinding(
        wakeup_callable=lambda conn, **kw: [],
        conn_factory=lambda: spy_conn,
        jobstore_url=_jobstore_url(tmp_path),
    )
    b._fire_for("a_schedule")
    assert spy_conn.closed is True


# ===========================================================================
# Hygiene smoke
# ===========================================================================


def test_binding_module_does_not_import_uuid():
    seen = set()
    for _, member in vars(binding_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    assert "uuid" not in seen


def test_binding_module_has_no_io_imports():
    """``apscheduler.jobstores.sqlalchemy`` pulls in
    SQLAlchemy (allowed -- it's the durable jobstore per
    design section 4.0.5). Forbid only the network /
    messaging libs that have no business in this module."""
    forbidden = {
        "httpx",
        "requests",
        "urllib.request",
        "urllib3",
        "aiohttp",
        "slack_sdk",
        "telegram",
        "googleapiclient",
        "google.cloud",
        "smtplib",
        "subprocess",
    }
    seen = set()
    for _, member in vars(binding_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"binding module imports unexpected libs: "
        f"{sorted(leaked)}"
    )


def test_binding_module_exposes_no_reasoning_or_emit_callables():
    forbidden = {
        "reason",
        "emit",
        "delegate",
        "transfer",
        "sub_agent",
        "dispatch",
        "invoke",
    }
    for name, member in vars(binding_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"binding module exposes execution-suggestive "
                f"callable: {name}"
            )


def test_binding_module_source_does_not_catch_base_exception():
    """Structural pin: ``_fire_for`` must catch Exception,
    NOT BaseException. The exact match avoids the
    docstring-mention false positive (the module docstring
    can mention ``BaseException`` for explanation as long as
    no handler line uses it).
    """
    source = inspect.getsource(binding_mod)
    # The forbidden form is the handler line literal.
    assert "except BaseException" not in source, (
        "binding must catch Exception, not BaseException -- "
        "CancelledError / shutdown signals must propagate."
    )


def test_fire_for_body_catches_exception_only():
    """AST pin: ``_fire_for`` contains an ``except Exception:``
    clause and NO ``except BaseException:`` clause. Walks the
    function AST so docstring substring noise doesn't apply."""
    import ast
    import textwrap

    source = textwrap.dedent(
        inspect.getsource(SchedulerBinding._fire_for)
    )
    tree = ast.parse(source)
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)

    saw_exception = False
    for node in ast.walk(func):
        if not isinstance(node, ast.ExceptHandler):
            continue
        et = node.type
        # ExceptHandler.type is either None (bare except),
        # a Name, or a Tuple of Names. Compare names.
        names: list[str] = []
        if isinstance(et, ast.Name):
            names = [et.id]
        elif isinstance(et, ast.Tuple):
            names = [
                e.id for e in et.elts if isinstance(e, ast.Name)
            ]
        for n in names:
            if n == "BaseException":
                pytest.fail(
                    f"_fire_for must not catch BaseException; "
                    f"found ``except {n}``"
                )
            if n == "Exception":
                saw_exception = True
    assert saw_exception, (
        "_fire_for must catch Exception in its body -- "
        "wakeup failures must be swallowed + logged so the "
        "scheduler isn't paused by a transient error."
    )
