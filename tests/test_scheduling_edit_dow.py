"""Pin the create- vs edit-path symmetry on cron day-of-week
validation.

The create path rejects numeric DOW because APScheduler's
``CronTrigger.from_crontab`` interprets ``0`` as Monday (non-standard;
most cron implementations use ``0`` as Sunday). Pre-2026-05-14 the
edit path skipped the check and rebuilt the trigger directly, so a
job that was frozen with a clean ``MON,WED,FRI`` schedule could be
edited to a numeric ``1,3,5`` and start firing on the wrong days.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _ctx(user_id: str = "tg_admin") -> MagicMock:
    ctx = MagicMock()
    ctx.session.state = {"user_id": user_id}
    return ctx


def test_edit_rejects_numeric_dow(monkeypatch):
    """The edit path must run ``_validate_cron_dow`` exactly like the
    create path. Numeric DOW → reject with a hint pointing at the
    3-letter alternative."""
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")

    from app.tools import scheduling as sched_mod

    # Pretend the scheduler has a recurring job we own.
    fake_job = MagicMock()
    fake_job.id = "cron_abcdef12"
    fake_job.kwargs = {"owner_user_id": "tg_admin"}
    fake_job.trigger = MagicMock()
    type(fake_job.trigger).__name__ = "CronTrigger"

    fake_scheduler = MagicMock()
    fake_scheduler.get_job.return_value = fake_job
    monkeypatch.setattr(sched_mod, "scheduler", fake_scheduler, raising=False)

    # Patch the module-level scheduler import inside the function too.
    monkeypatch.setattr(
        "app.scheduler_instance.scheduler", fake_scheduler, raising=False
    )

    res = sched_mod.edit_scheduled_task(
        job_id="cron_abcdef12",
        tool_context=_ctx(),
        new_cron_expression="0 18 * * 1,3,5",
        timezone="Europe/Kyiv",
    )

    assert res["status"] == "error"
    assert "Ambiguous day-of-week" in res["message"]
    # The job was NOT rescheduled — modify_job must not have been
    # called.
    fake_scheduler.reschedule_job.assert_not_called()


def test_edit_accepts_named_dow(monkeypatch):
    """Symmetric positive case: 3-letter DOW passes the validator and
    reaches the reschedule call."""
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")

    from app.tools import scheduling as sched_mod

    fake_job = MagicMock()
    fake_job.id = "cron_abcdef12"
    fake_job.kwargs = {
        "owner_user_id": "tg_admin",
        "task_prompt": "do thing",
        "notify": {},
    }
    type(fake_job.trigger).__name__ = "CronTrigger"

    fake_scheduler = MagicMock()
    fake_scheduler.get_job.return_value = fake_job
    monkeypatch.setattr(sched_mod, "scheduler", fake_scheduler, raising=False)
    monkeypatch.setattr(
        "app.scheduler_instance.scheduler", fake_scheduler, raising=False
    )

    res = sched_mod.edit_scheduled_task(
        job_id="cron_abcdef12",
        tool_context=_ctx(),
        new_cron_expression="0 18 * * MON,WED,FRI",
        timezone="Europe/Kyiv",
    )

    assert res["status"] == "success", res
    fake_scheduler.reschedule_job.assert_called_once()
