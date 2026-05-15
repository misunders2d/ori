"""Tests for ``app.v2.runtime.lifecycle``.

Plan section 5.4 pins. The hooks are pure delegations to
``SchedulerBinding``; tests use a MagicMock binding to
verify the delegation shape -- the binding's actual
behaviour is covered by tests/v2/test_runtime_binding.py.

Coverage:
- ``on_schedule_paused(binding, schedule_id)`` calls
  ``binding.unregister(schedule_id)``.
- ``on_schedule_archived(binding, schedule_id)`` calls
  ``binding.unregister(schedule_id)``.
- ``on_schedule_resumed(binding, spec)`` calls
  ``binding.register(spec)``.
- ``on_schedule_revised(binding, spec)`` calls
  ``binding.reregister(spec)``.

Module-surface pin:
- ``app.v2.runtime.lifecycle`` exposes ONLY the four
  documented public callables; no execution / fire /
  claim / reason / emit / delegate / transfer / sub_agent /
  dispatch / invoke callable.
- Module imports no I/O libs, no uuid.

Error-propagation pin:
- A binding method that raises must propagate through the
  hook -- the hooks do NOT swallow exceptions. Callers
  (authoring tools) decide how to surface the failure.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.v2.enums import (
    DeliveryFallbackPolicy,
    FailureActionType,
    ScheduleStatus,
)
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    UserRef,
)
from app.v2.models.schedule import ScheduleSpec
from app.v2.models.triggers import CronTrigger, OneOffTrigger
from app.v2.runtime import lifecycle as lifecycle_mod
from app.v2.runtime.lifecycle import (
    on_schedule_archived,
    on_schedule_paused,
    on_schedule_resumed,
    on_schedule_revised,
)


_NOW = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
_NOW_ISO = _NOW.isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(
    *,
    schedule_id="sched",
    trigger=None,
    status=ScheduleStatus.ACTIVE,
):
    if trigger is None:
        trigger = OneOffTrigger(
            at_iso_datetime=_NOW + timedelta(hours=1),
            timezone="UTC",
        )
    return ScheduleSpec(
        id=schedule_id,
        owner=UserRef(
            platform="telegram",
            user_id="330959414",
            display_name="Sergey",
        ),
        description="lifecycle test schedule",
        trigger=trigger,
        delivery=Delivery(
            target_session_id="sl_test",
            fallback_policy=DeliveryFallbackPolicy.SESSION_TO_ORIGIN,
        ),
        failure=FailurePolicy(
            on_failure_action=FailureActionType.ALERT_ADMIN,
        ),
        audit=AuditPolicy(),
        status=status,
        execution_plan_hash=None,
        authored_at=_NOW_ISO,
    ).with_fresh_hash()


def _binding_mock():
    """Mock SchedulerBinding with the four methods the
    hooks call. MagicMock catches every attribute access so
    a future hook that adds calls without test coverage
    surfaces here."""
    b = MagicMock(name="SchedulerBinding")
    return b


# ===========================================================================
# Hook delegation
# ===========================================================================


def test_on_schedule_paused_calls_unregister():
    b = _binding_mock()
    on_schedule_paused(b, "sched_x")
    b.unregister.assert_called_once_with("sched_x")
    # No other binding method touched.
    b.register.assert_not_called()
    b.reregister.assert_not_called()
    b.start.assert_not_called()
    b.stop.assert_not_called()
    b.resume.assert_not_called()


def test_on_schedule_archived_calls_unregister():
    b = _binding_mock()
    on_schedule_archived(b, "sched_x")
    b.unregister.assert_called_once_with("sched_x")
    b.register.assert_not_called()
    b.reregister.assert_not_called()


def test_on_schedule_resumed_calls_register():
    b = _binding_mock()
    spec = _spec(schedule_id="resume_me")
    on_schedule_resumed(b, spec)
    b.register.assert_called_once_with(spec)
    b.unregister.assert_not_called()
    b.reregister.assert_not_called()


def test_on_schedule_revised_calls_reregister():
    b = _binding_mock()
    spec = _spec(
        schedule_id="revise_me",
        trigger=CronTrigger(cron="0 18 * * *", timezone="UTC"),
    )
    on_schedule_revised(b, spec)
    b.reregister.assert_called_once_with(spec)
    b.register.assert_not_called()
    b.unregister.assert_not_called()


# ===========================================================================
# Error propagation -- hooks do NOT swallow exceptions
# ===========================================================================


def test_on_schedule_paused_propagates_unregister_errors():
    b = _binding_mock()
    b.unregister.side_effect = RuntimeError("synthetic unregister failure")
    with pytest.raises(RuntimeError, match="synthetic unregister failure"):
        on_schedule_paused(b, "x")


def test_on_schedule_archived_propagates_unregister_errors():
    b = _binding_mock()
    b.unregister.side_effect = RuntimeError("synthetic unregister failure")
    with pytest.raises(RuntimeError, match="synthetic unregister failure"):
        on_schedule_archived(b, "x")


def test_on_schedule_resumed_propagates_register_errors():
    b = _binding_mock()
    b.register.side_effect = ValueError("synthetic register failure")
    with pytest.raises(ValueError, match="synthetic register failure"):
        on_schedule_resumed(b, _spec(schedule_id="x"))


def test_on_schedule_revised_propagates_reregister_errors():
    b = _binding_mock()
    b.reregister.side_effect = ValueError("synthetic reregister failure")
    with pytest.raises(ValueError, match="synthetic reregister failure"):
        on_schedule_revised(b, _spec(schedule_id="x"))


# ===========================================================================
# Module surface pin
# ===========================================================================


def test_lifecycle_module_exposes_exactly_four_hooks():
    """Plan section 3.4 lists EXACTLY four public hooks.
    Pin so a future helper added to the module without an
    explicit review surfaces."""
    expected = {
        "on_schedule_archived",
        "on_schedule_paused",
        "on_schedule_resumed",
        "on_schedule_revised",
    }
    assert set(lifecycle_mod.__all__) == expected
    for name in expected:
        assert hasattr(lifecycle_mod, name)
        assert callable(getattr(lifecycle_mod, name))


def test_lifecycle_module_exposes_no_execution_surface():
    """The module is pure-delegation to the binding. No
    reasoning / emit / fire / claim / dispatch surface."""
    forbidden = {
        "reason",
        "emit",
        "delegate",
        "transfer",
        "sub_agent",
        "dispatch",
        "invoke",
        "fire",
        "claim",
        "execute",
        "run",
    }
    for name, member in vars(lifecycle_mod).items():
        if name.startswith("_"):
            continue
        if callable(member) and name in forbidden:
            pytest.fail(
                f"lifecycle module exposes execution-suggestive "
                f"callable: {name}"
            )


def test_lifecycle_module_has_no_io_imports():
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
        "sqlite3",  # hooks are pure -- no direct DB access
    }
    seen = set()
    for _, member in vars(lifecycle_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    leaked = seen & forbidden
    assert not leaked, (
        f"lifecycle module imports unexpected libs: {sorted(leaked)}"
    )


def test_lifecycle_module_does_not_import_uuid():
    """Hooks delegate to the binding for everything; they
    have no business with uuid."""
    seen = set()
    for _, member in vars(lifecycle_mod).items():
        if inspect.ismodule(member):
            seen.add(member.__name__)
    assert "uuid" not in seen


# ===========================================================================
# Hook idempotency / signature shape
# ===========================================================================


def test_paused_hook_signature():
    """``on_schedule_paused(binding, schedule_id: str)``."""
    sig = inspect.signature(on_schedule_paused)
    params = list(sig.parameters.values())
    assert len(params) == 2
    assert params[0].name == "binding"
    assert params[1].name == "schedule_id"


def test_archived_hook_signature():
    sig = inspect.signature(on_schedule_archived)
    params = list(sig.parameters.values())
    assert len(params) == 2
    assert params[0].name == "binding"
    assert params[1].name == "schedule_id"


def test_resumed_hook_signature():
    """``on_schedule_resumed(binding, spec: ScheduleSpec)``."""
    sig = inspect.signature(on_schedule_resumed)
    params = list(sig.parameters.values())
    assert len(params) == 2
    assert params[0].name == "binding"
    assert params[1].name == "spec"


def test_revised_hook_signature():
    sig = inspect.signature(on_schedule_revised)
    params = list(sig.parameters.values())
    assert len(params) == 2
    assert params[0].name == "binding"
    assert params[1].name == "spec"


def test_hooks_are_sync_callables():
    """The lifecycle hooks are sync -- they delegate to
    binding methods that are sync from the caller's
    perspective (register / unregister / reregister are
    all sync). Pin so a future refactor that makes them
    async breaks here (would need careful migration of
    every authoring-tool call site)."""
    for fn in (
        on_schedule_paused,
        on_schedule_archived,
        on_schedule_resumed,
        on_schedule_revised,
    ):
        assert not inspect.iscoroutinefunction(fn), (
            f"{fn.__name__} must stay sync"
        )
