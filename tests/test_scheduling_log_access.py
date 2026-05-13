"""Access-control tests for ``get_scheduled_task_logs``.

Pre-2026-05-14 the tool returned ALL events to any caller, embedding
prompt_preview, response_preview, and channel metadata from every
user's scheduled tasks. Non-admins could trivially scrape other
users' task content from a shared log file. This suite pins the new
gate:

  * Admins see everything (per-task or global).
  * Non-admins MUST supply a ``task_id`` and may only see events for
    tasks they own.
  * Legacy events without an ``owner_user_id`` stamp are admin-only.
  * System-kind tasks are admin-only regardless of owner.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_log(tmp_path, monkeypatch):
    """Redirect the scheduler job log to a per-test file and seed
    representative events."""
    log_path = tmp_path / "scheduler_jobs.log"

    # Monkeypatch the absolute-path lookup inside get_scheduled_task_logs.
    real_abspath = os.path.abspath

    def _fake_abspath(p):
        if p.endswith("scheduler_jobs.log"):
            return str(log_path)
        return real_abspath(p)

    monkeypatch.setattr(os.path, "abspath", _fake_abspath)
    return log_path


def _seed(log_path, events: list[dict]) -> None:
    with open(log_path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _ctx(user_id: str) -> MagicMock:
    """Build a ToolContext-shaped mock for the scheduling module's
    ``_get_user_id`` helper, which reads
    ``tool_context.session.state["user_id"]``."""
    ctx = MagicMock()
    ctx.session.state = {"user_id": user_id}
    return ctx


# ---------------------------------------------------------------------------
# Admin path
# ---------------------------------------------------------------------------


def test_admin_sees_all_events_with_no_task_id(fake_log, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    _seed(
        fake_log,
        [
            {
                "ts": "2026-05-13T17:00:00",
                "event": "fire_start",
                "task_id": "sched_a",
                "kind": "scheduled",
                "owner_user_id": "tg_user_a",
                "prompt_preview": "user A secret",
            },
            {
                "ts": "2026-05-13T17:01:00",
                "event": "fire_start",
                "task_id": "sched_b",
                "kind": "scheduled",
                "owner_user_id": "tg_user_b",
                "prompt_preview": "user B secret",
            },
        ],
    )

    from app.tools.scheduling import get_scheduled_task_logs

    res = get_scheduled_task_logs(_ctx("tg_admin"))
    assert res["status"] == "success"
    assert res["count"] == 2


def test_admin_sees_system_task_events(fake_log, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    _seed(
        fake_log,
        [
            {
                "ts": "2026-05-13T17:00:00",
                "event": "fire_start",
                "task_id": "sys_x",
                "kind": "system",
                "admin_user_id": "tg_admin",
            }
        ],
    )

    from app.tools.scheduling import get_scheduled_task_logs

    res = get_scheduled_task_logs(_ctx("tg_admin"), task_id="sys_x")
    assert res["status"] == "success"
    assert res["count"] == 1


# ---------------------------------------------------------------------------
# Non-admin path
# ---------------------------------------------------------------------------


def test_non_admin_without_task_id_denied(fake_log, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    _seed(fake_log, [])

    from app.tools.scheduling import get_scheduled_task_logs

    res = get_scheduled_task_logs(_ctx("tg_user_a"))
    assert res["status"] == "error"
    assert "non-admin" in res["message"].lower()


def test_non_admin_sees_own_task(fake_log, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    _seed(
        fake_log,
        [
            {
                "ts": "2026-05-13T17:00:00",
                "event": "fire_start",
                "task_id": "sched_a",
                "kind": "scheduled",
                "owner_user_id": "tg_user_a",
                "prompt_preview": "ok",
            },
            {
                "ts": "2026-05-13T17:00:30",
                "event": "fire_end",
                "task_id": "sched_a",
                "kind": "scheduled",
                "owner_user_id": "tg_user_a",
                "status": "Completed",
            },
        ],
    )

    from app.tools.scheduling import get_scheduled_task_logs

    res = get_scheduled_task_logs(_ctx("tg_user_a"), task_id="sched_a")
    assert res["status"] == "success"
    assert res["count"] == 2


def test_non_admin_denied_for_other_user_task(fake_log, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    _seed(
        fake_log,
        [
            {
                "ts": "2026-05-13T17:00:00",
                "event": "fire_start",
                "task_id": "sched_b",
                "kind": "scheduled",
                "owner_user_id": "tg_user_b",
                "prompt_preview": "B's secret",
            }
        ],
    )

    from app.tools.scheduling import get_scheduled_task_logs

    res = get_scheduled_task_logs(_ctx("tg_user_a"), task_id="sched_b")
    assert res["status"] == "error"
    assert "owned by another" in res["message"]


def test_non_admin_denied_for_system_task(fake_log, monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    _seed(
        fake_log,
        [
            {
                "ts": "2026-05-13T17:00:00",
                "event": "fire_start",
                "task_id": "sys_x",
                "kind": "system",
                "admin_user_id": "tg_admin",
            }
        ],
    )

    from app.tools.scheduling import get_scheduled_task_logs

    res = get_scheduled_task_logs(_ctx("tg_user_a"), task_id="sys_x")
    assert res["status"] == "error"
    assert "admin-only" in res["message"]


def test_non_admin_denied_for_legacy_unstamped_task(fake_log, monkeypatch):
    """Pre-2026-05-14 events have no ``owner_user_id``. The gate
    treats them as admin-only — we can't verify ownership."""
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")
    _seed(
        fake_log,
        [
            {
                "ts": "2026-05-12T10:00:00",
                "event": "fire_start",
                "task_id": "sched_legacy",
                "kind": "scheduled",
                # No owner_user_id field.
                "prompt_preview": "legacy task",
            }
        ],
    )

    from app.tools.scheduling import get_scheduled_task_logs

    res = get_scheduled_task_logs(_ctx("tg_user_a"), task_id="sched_legacy")
    assert res["status"] == "error"
    assert "no recorded owner" in res["message"]
