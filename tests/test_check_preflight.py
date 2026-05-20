"""Tests for ``scripts/check_preflight.py``.

The script is a pre-push preflight that runs five independent
sections and produces a single non-zero exit if any section ends
``[FAIL]``. Tests cover:

* Happy-path: every section returns OK → exit 0.
* Boot validator ghost finding → FAIL, propagates to exit 1.
* Risky-cron audit hit (no ghost) → FAIL.
* Per-job trigger audit: WARN by default, FAIL under ``--strict``.
* Pytest non-zero exit → propagates to exit 1.
* Working tree dirty in ``app/`` or ``docs/`` → WARN (not FAIL).
* Untracked files alone → still OK (no warning escalation).
* Git log empty (nothing ahead) → WARN (not FAIL).
* Git log unexpectedly large → WARN.

Mocks ``app.agent.root_agent`` + ``app.scheduler_instance.scheduler``
+ ``app.core.instruction_validator`` helpers + ``app.tasks._match_triggers``
+ ``subprocess.run`` so the tests run in milliseconds and never
shell out to git or pytest.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module loader — script lives under ``scripts/`` and isn't on sys.path
# ---------------------------------------------------------------------------


_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT_PATH = os.path.abspath(os.path.join(_HERE, "..", "scripts", "check_preflight.py"))


def _load_preflight():
    spec = importlib.util.spec_from_file_location("check_preflight", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def preflight():
    return _load_preflight()


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------


def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# Section 1 — boot validator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_section_boot_validator_clean(preflight):
    with patch("app.core.instruction_validator.validate_agent_tool_refs",
               new=AsyncMock(return_value=[])):
        with patch("app.core.instruction_validator.audit_persisted_jobs",
                   return_value=[]):
            with patch.object(preflight, "_read_persisted_jobs",
                              return_value=[]):  # jobstore introspected, zero jobs
                status, body = await preflight.section_boot_validator()
    assert status == preflight.OK
    assert "no ghost-tool" in body.lower()
    assert "0 job(s)" in body


@pytest.mark.asyncio
async def test_section_boot_validator_persisted_jobs_unavailable_warns(preflight):
    """Reviewer blocker: a stopped scheduler returns [] from
    ``get_jobs()``, which previously read as 'clean'. Now the
    jobstore-introspection path returns None when it can't read the
    store, and the section emits WARN instead of OK."""
    with patch("app.core.instruction_validator.validate_agent_tool_refs",
               new=AsyncMock(return_value=[])):
        with patch("app.core.instruction_validator.audit_persisted_jobs",
                   return_value=[]):
            with patch.object(preflight, "_read_persisted_jobs",
                              return_value=None):
                status, body = await preflight.section_boot_validator()
    assert status == preflight.WARN
    assert "SKIPPED" in body
    assert "jobstore" in body.lower()


@pytest.mark.asyncio
async def test_section_boot_validator_ghost_finding_fails(preflight):
    finding = {
        "agent": "Coord", "source": "instruction",
        "unresolved": ["drive_upload_file"],
    }
    with patch("app.core.instruction_validator.validate_agent_tool_refs",
               new=AsyncMock(return_value=[finding])):
        with patch("app.core.instruction_validator.audit_persisted_jobs",
                   return_value=[]):
            with patch.object(preflight, "_read_persisted_jobs",
                              return_value=[]):
                status, body = await preflight.section_boot_validator()
    assert status == preflight.FAIL
    assert "drive_upload_file" in body


@pytest.mark.asyncio
async def test_section_boot_validator_risky_cron_fails(preflight):
    """Audit hit alone (no ghost findings) still fails section 1."""
    audit = [{
        "job_id": "cron_97f22322",
        "matched": ["`reports.business_report_asin`"],
        "kinds": ["required_any"],
    }]
    with patch("app.core.instruction_validator.validate_agent_tool_refs",
               new=AsyncMock(return_value=[])):
        with patch("app.core.instruction_validator.audit_persisted_jobs",
                   return_value=audit):
            with patch.object(preflight, "_read_persisted_jobs",
                              return_value=[SimpleNamespace(id="cron_97f22322")]):
                status, body = await preflight.section_boot_validator()
    assert status == preflight.FAIL
    assert "cron_97f22322" in body


@pytest.mark.asyncio
async def test_section_boot_validator_validate_raise_fails(preflight):
    with patch("app.core.instruction_validator.validate_agent_tool_refs",
               new=AsyncMock(side_effect=RuntimeError("boom"))):
        status, body = await preflight.section_boot_validator()
    assert status == preflight.FAIL
    assert "validate_agent_tool_refs raised" in body


@pytest.mark.asyncio
async def test_section_boot_validator_audit_raise_fails(preflight):
    with patch("app.core.instruction_validator.validate_agent_tool_refs",
               new=AsyncMock(return_value=[])):
        with patch("app.core.instruction_validator.audit_persisted_jobs",
                   side_effect=RuntimeError("scheduler boom")):
            with patch.object(preflight, "_read_persisted_jobs",
                              return_value=[]):
                status, body = await preflight.section_boot_validator()
    assert status == preflight.FAIL
    assert "audit_persisted_jobs raised" in body


# ---------------------------------------------------------------------------
# Section 2 — per-job trigger audit
# ---------------------------------------------------------------------------


def _job(job_id: str, prompt: str) -> SimpleNamespace:
    return SimpleNamespace(id=job_id, kwargs={"task_prompt": prompt})


def test_section_per_job_audit_no_jobs(preflight):
    with patch.object(preflight, "_read_persisted_jobs", return_value=[]):
        status, body = preflight.section_per_job_trigger_audit(strict=False)
    assert status == preflight.OK
    assert "no persisted jobs" in body


def test_section_per_job_audit_no_hits(preflight):
    with patch.object(preflight, "_read_persisted_jobs",
                      return_value=[_job("cron_x", "Remind me at 4pm.")]):
        status, body = preflight.section_per_job_trigger_audit(strict=False)
    assert status == preflight.OK
    assert "no trigger matches" in body


def test_section_per_job_audit_hits_warn_default(preflight):
    jobs = [_job("cron_97f22322", "Query `reports.business_report_asin`.")]
    with patch.object(preflight, "_read_persisted_jobs", return_value=jobs):
        status, body = preflight.section_per_job_trigger_audit(strict=False)
    assert status == preflight.WARN
    assert "cron_97f22322" in body


def test_section_per_job_audit_hits_fail_strict(preflight):
    jobs = [_job("cron_97f22322", "Query `reports.business_report_asin`.")]
    with patch.object(preflight, "_read_persisted_jobs", return_value=jobs):
        status, body = preflight.section_per_job_trigger_audit(strict=True)
    assert status == preflight.FAIL
    assert "cron_97f22322" in body


def test_section_per_job_audit_jobstore_unavailable_warns(preflight):
    """Reviewer regression: stopped scheduler / clean dev clone →
    ``_read_persisted_jobs`` returns None → audit cannot run.
    Section emits WARN (not OK), so the operator knows the audit was
    SKIPPED, not clean."""
    with patch.object(preflight, "_read_persisted_jobs", return_value=None):
        status, body = preflight.section_per_job_trigger_audit(strict=False)
    assert status == preflight.WARN
    assert "SKIPPED" in body
    assert "jobstore" in body.lower()


def test_section_per_job_audit_skips_blank_prompts(preflight):
    """Job with empty task_prompt → skipped, not flagged."""
    with patch.object(preflight, "_read_persisted_jobs",
                      return_value=[_job("cron_blank", "")]):
        status, body = preflight.section_per_job_trigger_audit(strict=False)
    assert status == preflight.OK


# ---------------------------------------------------------------------------
# _read_persisted_jobs — direct tests
# ---------------------------------------------------------------------------


def test_read_persisted_jobs_uses_jobstore_get_all_jobs(preflight):
    """Reviewer regression: read SQLAlchemyJobStore DIRECTLY rather
    than via the stopped scheduler's ``get_jobs()`` (which returns []).
    Verify we call the jobstore's ``get_all_jobs`` after a defensive
    ``start`` (idempotent)."""
    fake_job_a = SimpleNamespace(id="cron_a", kwargs={})
    fake_job_b = SimpleNamespace(id="cron_b", kwargs={})

    jobstore = MagicMock()
    jobstore.get_all_jobs.return_value = [fake_job_a, fake_job_b]
    sched = SimpleNamespace(_jobstores={"default": jobstore})

    out = preflight._read_persisted_jobs(sched)
    assert out == [fake_job_a, fake_job_b]
    jobstore.start.assert_called_once_with(sched, "default")
    jobstore.get_all_jobs.assert_called_once()


def test_read_persisted_jobs_start_failure_is_swallowed(preflight):
    """Some APScheduler versions raise on duplicate start; the
    helper ignores start errors and still calls get_all_jobs."""
    jobstore = MagicMock()
    jobstore.start.side_effect = RuntimeError("already started")
    jobstore.get_all_jobs.return_value = []
    sched = SimpleNamespace(_jobstores={"default": jobstore})

    out = preflight._read_persisted_jobs(sched)
    assert out == []
    jobstore.get_all_jobs.assert_called_once()


def test_read_persisted_jobs_returns_none_when_no_jobstores(preflight):
    sched = SimpleNamespace(_jobstores=None)
    assert preflight._read_persisted_jobs(sched) is None


def test_read_persisted_jobs_returns_none_when_no_default_store(preflight):
    sched = SimpleNamespace(_jobstores={"other": MagicMock()})
    assert preflight._read_persisted_jobs(sched) is None


def test_read_persisted_jobs_returns_none_on_get_all_jobs_failure(preflight):
    jobstore = MagicMock()
    jobstore.get_all_jobs.side_effect = RuntimeError("DB connection lost")
    sched = SimpleNamespace(_jobstores={"default": jobstore})
    assert preflight._read_persisted_jobs(sched) is None


def test_read_persisted_jobs_returns_none_when_get_all_jobs_missing(preflight):
    """Jobstore missing get_all_jobs entirely (unusual but possible
    on a custom jobstore implementation)."""
    jobstore = SimpleNamespace(start=lambda *a, **kw: None)
    sched = SimpleNamespace(_jobstores={"default": jobstore})
    assert preflight._read_persisted_jobs(sched) is None


def test_scheduler_shim_round_trips_job_list(preflight):
    """The shim's get_jobs() returns the exact list it was constructed
    with — lets audit_persisted_jobs(shim) see persisted rows even
    though the real scheduler isn't running."""
    jobs = [SimpleNamespace(id="a"), SimpleNamespace(id="b")]
    shim = preflight._SchedulerShim(jobs)
    assert shim.get_jobs() == jobs


# ---------------------------------------------------------------------------
# Section 3 — pytest
# ---------------------------------------------------------------------------


def test_section_pytest_pass(preflight):
    with patch("subprocess.run",
               return_value=_fake_proc(0, stdout="...\n100 passed in 1.2s\n")):
        status, body = preflight.section_pytest()
    assert status == preflight.OK
    assert "100 passed" in body


def test_section_pytest_fail_propagates(preflight):
    with patch("subprocess.run",
               return_value=_fake_proc(
                   1,
                   stdout="...\n1 failed, 99 passed\n",
                   stderr="E   AssertionError",
               )):
        status, body = preflight.section_pytest()
    assert status == preflight.FAIL
    assert "pytest exit 1" in body
    assert "AssertionError" in body


def test_section_pytest_uv_missing(preflight):
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        status, body = preflight.section_pytest()
    assert status == preflight.FAIL
    assert "uv" in body.lower()


# ---------------------------------------------------------------------------
# Section 4 — working tree
# ---------------------------------------------------------------------------


def test_section_working_tree_clean(preflight):
    with patch("subprocess.run", return_value=_fake_proc(0, stdout="")):
        status, body = preflight.section_working_tree()
    assert status == preflight.OK
    assert "clean" in body.lower()


def test_section_working_tree_app_drift_warns(preflight):
    out = " M app/tools/example.py\n"
    with patch("subprocess.run", return_value=_fake_proc(0, stdout=out)):
        status, body = preflight.section_working_tree()
    assert status == preflight.WARN
    assert "app/tools/example.py" in body


def test_section_working_tree_docs_drift_warns(preflight):
    out = " M docs/RUNBOOK.md\n"
    with patch("subprocess.run", return_value=_fake_proc(0, stdout=out)):
        status, body = preflight.section_working_tree()
    assert status == preflight.WARN
    assert "RUNBOOK" in body


def test_section_working_tree_untracked_only_does_not_warn(preflight):
    """Untracked-only state stays OK — common during dev."""
    out = "?? .pi/\n?? something.txt\n"
    with patch("subprocess.run", return_value=_fake_proc(0, stdout=out)):
        status, body = preflight.section_working_tree()
    assert status == preflight.OK
    assert "untracked" in body.lower()
    assert ".pi/" in body


def test_section_working_tree_git_missing(preflight):
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        status, body = preflight.section_working_tree()
    assert status == preflight.FAIL
    assert "git" in body.lower()


# ---------------------------------------------------------------------------
# Section 5 — recent commits
# ---------------------------------------------------------------------------


def test_section_recent_commits_in_window(preflight):
    # 8 commits, inside the 5..30 window → OK.
    log_out = "\n".join(f"abc{i:04d} feat: thing {i}" for i in range(8)) + "\n"
    with patch("subprocess.run", return_value=_fake_proc(0, stdout=log_out)):
        status, body = preflight.section_recent_commits()
    assert status == preflight.OK
    assert "8 commit" in body


def test_section_recent_commits_empty_warns(preflight):
    with patch("subprocess.run", return_value=_fake_proc(0, stdout="")):
        status, body = preflight.section_recent_commits()
    assert status == preflight.WARN
    assert "nothing to push" in body.lower()


def test_section_recent_commits_huge_delta_warns(preflight):
    log_out = "\n".join(f"hash{i:04d} feat: thing {i}" for i in range(60)) + "\n"
    with patch("subprocess.run", return_value=_fake_proc(0, stdout=log_out)):
        status, body = preflight.section_recent_commits()
    assert status == preflight.WARN
    assert "unexpected commit count" in body


def test_section_recent_commits_remote_ref_missing(preflight):
    err = subprocess.CalledProcessError(
        128, ["git", "log"], stderr="unknown revision origin/x",
    )
    with patch("subprocess.run", side_effect=err):
        status, body = preflight.section_recent_commits()
    assert status == preflight.WARN
    assert "git log" in body


# ---------------------------------------------------------------------------
# Orchestrator: run_all + exit codes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_all_clean_returns_zero(preflight, capsys):
    """Smoke happy-path — all sections OK → exit 0."""
    fakes = {
        "section_boot_validator": AsyncMock(return_value=(preflight.OK, "clean")),
        "section_per_job_trigger_audit": MagicMock(return_value=(preflight.OK, "clean")),
        "section_pytest": MagicMock(return_value=(preflight.OK, "1000 passed")),
        "section_working_tree": MagicMock(return_value=(preflight.OK, "clean")),
        "section_recent_commits": MagicMock(return_value=(preflight.OK, "8 commits")),
    }
    with patch.multiple(preflight, **fakes):
        rc = await preflight.run_all(strict=False, ref="origin/evo/amazon_manager")
    assert rc == 0
    out = capsys.readouterr().out
    assert "preflight clean" in out.lower()


@pytest.mark.asyncio
async def test_run_all_boot_finding_returns_one(preflight, capsys):
    with patch.multiple(
        preflight,
        section_boot_validator=AsyncMock(return_value=(preflight.FAIL, "ghost")),
        section_per_job_trigger_audit=MagicMock(return_value=(preflight.OK, "")),
        section_pytest=MagicMock(return_value=(preflight.OK, "")),
        section_working_tree=MagicMock(return_value=(preflight.OK, "")),
        section_recent_commits=MagicMock(return_value=(preflight.OK, "")),
    ):
        rc = await preflight.run_all(strict=False, ref="origin/evo/amazon_manager")
    assert rc == 1
    out = capsys.readouterr().out
    assert "preflight failed" in out.lower()


@pytest.mark.asyncio
async def test_run_all_warn_only_returns_zero(preflight, capsys):
    """A WARN alone does not force non-zero exit."""
    with patch.multiple(
        preflight,
        section_boot_validator=AsyncMock(return_value=(preflight.OK, "")),
        section_per_job_trigger_audit=MagicMock(return_value=(preflight.WARN, "1 hit")),
        section_pytest=MagicMock(return_value=(preflight.OK, "")),
        section_working_tree=MagicMock(return_value=(preflight.OK, "")),
        section_recent_commits=MagicMock(return_value=(preflight.OK, "")),
    ):
        rc = await preflight.run_all(strict=False, ref="origin/evo/amazon_manager")
    assert rc == 0
    out = capsys.readouterr().out
    assert "with warnings" in out.lower()


@pytest.mark.asyncio
async def test_run_all_pytest_fail_returns_one(preflight, capsys):
    with patch.multiple(
        preflight,
        section_boot_validator=AsyncMock(return_value=(preflight.OK, "")),
        section_per_job_trigger_audit=MagicMock(return_value=(preflight.OK, "")),
        section_pytest=MagicMock(return_value=(preflight.FAIL, "exit 1")),
        section_working_tree=MagicMock(return_value=(preflight.OK, "")),
        section_recent_commits=MagicMock(return_value=(preflight.OK, "")),
    ):
        rc = await preflight.run_all(strict=False, ref="origin/evo/amazon_manager")
    assert rc == 1


@pytest.mark.asyncio
async def test_run_all_dirty_app_warns_but_zero(preflight, capsys):
    with patch.multiple(
        preflight,
        section_boot_validator=AsyncMock(return_value=(preflight.OK, "")),
        section_per_job_trigger_audit=MagicMock(return_value=(preflight.OK, "")),
        section_pytest=MagicMock(return_value=(preflight.OK, "")),
        section_working_tree=MagicMock(return_value=(preflight.WARN, "M app/x")),
        section_recent_commits=MagicMock(return_value=(preflight.OK, "")),
    ):
        rc = await preflight.run_all(strict=False, ref="origin/evo/amazon_manager")
    assert rc == 0
    out = capsys.readouterr().out
    assert "with warnings" in out.lower()


# ---------------------------------------------------------------------------
# Argparse entrypoint
# ---------------------------------------------------------------------------


def test_main_accepts_strict_and_ref(preflight):
    with patch.object(preflight, "run_all",
                      new=AsyncMock(return_value=0)) as mock_runner:
        rc = preflight.main(
            ["--strict", "--ref", "origin/main"]
        )
    assert rc == 0
    assert mock_runner.await_count == 1
    kwargs = mock_runner.call_args.kwargs
    assert kwargs["strict"] is True
    assert kwargs["ref"] == "origin/main"


def test_main_defaults_no_strict_main_ref(preflight):
    with patch.object(preflight, "run_all",
                      new=AsyncMock(return_value=0)) as mock_runner:
        preflight.main([])
    kwargs = mock_runner.call_args.kwargs
    assert kwargs["strict"] is False
    assert kwargs["ref"] == "origin/evo/amazon_manager"
