"""Executor tests — schedule_contract wiring, on-demand bypass, hash
pinning, prefix isolation from legacy jobs.

We mock APScheduler entirely; this layer is just plumbing.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.contracts import executor as exec_mod
from app.contracts.schema import (
    Contract,
    CronTrigger,
    EmitStep,
    EventTrigger,
    OnDemandTrigger,
)


def _make_contract(trigger=None, **overrides) -> Contract:
    base = dict(
        id="t_exec",
        description="exec test",
        author="t",
        trigger=trigger or CronTrigger(cron="0 0 * * *", timezone="UTC"),
        emit=[EmitStep(adapter="slack_post", args={"channel": "#x", "content": "y"})],
    )
    base.update(overrides)
    return Contract(**base).with_fresh_hash()


@pytest.fixture
def mock_scheduler(monkeypatch):
    """Patch ``app.scheduler_instance.scheduler`` with a MagicMock so
    we can assert exactly which APScheduler methods got called with
    which args."""
    mock = MagicMock()
    monkeypatch.setattr("app.scheduler_instance.scheduler", mock)
    return mock


# ---------------------------------------------------------------------------
# Cron trigger → APScheduler add_job
# ---------------------------------------------------------------------------


def test_schedule_cron_adds_job_with_contract_prefix(mock_scheduler):
    """The job id must be ``contract:<id>`` so the executor and the
    legacy ``cron_*`` scheduler can coexist without colliding."""
    c = _make_contract()

    result = exec_mod.schedule_contract(c)

    assert result["status"] == "scheduled"
    assert result["job_id"] == f"contract:{c.id}"
    mock_scheduler.add_job.assert_called_once()
    call = mock_scheduler.add_job.call_args
    assert call.kwargs["id"] == f"contract:{c.id}"
    # Hash is pinned into the job kwargs so the fire callback knows
    # which body to load.
    assert call.kwargs["kwargs"]["hash_"] == c.hash
    assert call.kwargs["kwargs"]["contract_id"] == c.id


def test_schedule_removes_prior_job_before_re_adding(mock_scheduler):
    """Revising a contract reuses the same job id; re-scheduling must
    drop the old job before adding the new one (atomic switch)."""
    c = _make_contract()
    exec_mod.schedule_contract(c)
    mock_scheduler.remove_job.assert_called_once_with(f"contract:{c.id}")


def test_schedule_with_on_demand_trigger_skips_apscheduler(mock_scheduler):
    """on_demand contracts fire only via direct invocation — we must
    NOT put them in APScheduler (would never fire and would clutter
    the job list)."""
    c = _make_contract(trigger=OnDemandTrigger())

    result = exec_mod.schedule_contract(c)
    assert result["status"] == "on_demand"
    mock_scheduler.add_job.assert_not_called()


def test_schedule_with_event_trigger_skips_apscheduler(mock_scheduler):
    """Event triggers are reserved for follow-up event-bus wiring —
    they shouldn't end up in APScheduler either."""
    c = _make_contract(trigger=EventTrigger(event="asin_audit_requested"))

    result = exec_mod.schedule_contract(c)
    assert result["status"] == "event"
    mock_scheduler.add_job.assert_not_called()


# ---------------------------------------------------------------------------
# unschedule
# ---------------------------------------------------------------------------


def test_unschedule_removes_job(mock_scheduler):
    result = exec_mod.unschedule_contract("daily_tip")
    assert result["status"] == "removed"
    assert result["job_id"] == "contract:daily_tip"
    mock_scheduler.remove_job.assert_called_once_with("contract:daily_tip")


def test_unschedule_reports_not_found(mock_scheduler):
    """APScheduler raises ``JobLookupError`` for missing ids; we
    surface it as ``not_found`` rather than re-raising so the agent
    can react cleanly."""
    mock_scheduler.remove_job.side_effect = RuntimeError("job not found")
    result = exec_mod.unschedule_contract("ghost")
    assert result["status"] == "not_found"


# ---------------------------------------------------------------------------
# list_contract_jobs
# ---------------------------------------------------------------------------


def test_list_contract_jobs_filters_by_prefix(mock_scheduler):
    """Only ``contract:*`` jobs are returned. Legacy ``cron_*`` /
    ``oneoff_*`` jobs stay invisible to this listing."""
    legacy = MagicMock(id="cron_abc", kwargs={}, next_run_time=None)
    legacy.__class__.__str__ = lambda s: s.id
    new = MagicMock(id="contract:daily_tip", kwargs={"hash_": "abc"}, next_run_time=None)
    new.__class__.__str__ = lambda s: s.id
    mock_scheduler.get_jobs.return_value = [legacy, new]

    out = exec_mod.list_contract_jobs()
    assert len(out) == 1
    assert out[0]["job_id"] == "contract:daily_tip"
    assert out[0]["contract_id"] == "daily_tip"
    assert out[0]["hash"] == "abc"


# ---------------------------------------------------------------------------
# contract_job_id helper
# ---------------------------------------------------------------------------


def test_contract_job_id_prefix():
    assert exec_mod.contract_job_id("foo") == "contract:foo"
