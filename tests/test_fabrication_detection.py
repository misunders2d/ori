"""Tests for the fabrication detection wiring in
``app.tasks._check_fabrication_for_scheduled_fire`` (slice 3).

The helper composes ``_match_triggers`` (slice 2) with
``list_invocation_tool_calls`` (slice 1) and the existing
``run_scheduled_task`` state-mutation surface. Behaviour under test:

* No triggers in the prompt → returns the response unchanged and
  ``agent_ok = True``.
* Triggers + the required tool group is satisfied by at least one
  call → response unchanged, ``agent_ok = True``.
* Triggers + the required tool group is unsatisfied → response
  prefixed with the fabrication warning, ``agent_ok = False``,
  status updated to ``"Failed (fabrication detected: ...)"``,
  ``_log_job_event("error", ...)`` written, ``notify_admins``
  called, and severity is ``logger.error`` (not WARNING).
* Un-runnable trigger (Drive upload) → same fail-the-task posture
  but severity is ``logger.critical`` (no satisfying tool can ever
  exist in this build).
* Event scan failure → fail-the-task, ``logger.critical`` (in the
  helper) + ``notify_admins`` + response prefixed with the
  "cannot self-verify" notice.
* Admin alert failure during the Rule-13 cascade does not blow up
  the helper.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.tasks as tasks
from app.tasks import (
    ACTIVE_TASKS,
    _check_fabrication_for_scheduled_fire,
    _notify_admins_safe,
)


@pytest.fixture(autouse=True)
def reset_active_tasks():
    ACTIVE_TASKS.clear()
    yield
    ACTIVE_TASKS.clear()


def _seed_active(task_id: str) -> None:
    ACTIVE_TASKS[task_id] = {
        "prompt": "<prompt>",
        "type": "scheduled",
        "status": "Completed",
        "start_time": "2026-05-20T11:30:00",
        "end_time": "2026-05-20T11:30:14",
        "error": None,
    }


def _runner_with_calls(call_names: list[str]) -> MagicMock:
    """Build a runner whose session-event scan returns the given
    function_call names (and no responses)."""
    parts = []
    for name in call_names:
        parts.append(SimpleNamespace(
            function_call=SimpleNamespace(name=name, args={}),
            function_response=None,
        ))
    content = SimpleNamespace(parts=parts)
    event = SimpleNamespace(timestamp=2000.0, content=content)
    session = MagicMock()
    session.events = [event]
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    runner.session_service.get_session.return_value = session
    return runner


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_triggers_returns_response_unchanged():
    _seed_active("sched_a")
    runner = _runner_with_calls([])
    out_resp, out_ok = await _check_fabrication_for_scheduled_fire(
        runner=runner, user_id="u", session_id="s",
        task_id="sched_a", owner_user_id="o",
        task_prompt="Remind me about the call at 4pm",
        response="Reminder sent.", start_epoch=1000.0,
    )
    assert out_resp == "Reminder sent."
    assert out_ok is True
    assert ACTIVE_TASKS["sched_a"]["status"] == "Completed"


@pytest.mark.asyncio
async def test_required_group_satisfied_returns_unchanged():
    _seed_active("sched_b")
    runner = _runner_with_calls(["execute_sql", "sheets_write"])
    prompt = "Query `reports.business_report_asin` and write to the spreadsheet."
    out_resp, out_ok = await _check_fabrication_for_scheduled_fire(
        runner=runner, user_id="u", session_id="s",
        task_id="sched_b", owner_user_id="o",
        task_prompt=prompt, response="Row appended.",
        start_epoch=1000.0,
    )
    assert out_ok is True
    assert "fabrication" not in out_resp.lower()
    assert ACTIVE_TASKS["sched_b"]["status"] == "Completed"


# ---------------------------------------------------------------------------
# Fail-the-task: required_any group not satisfied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_required_any_not_satisfied_flips_agent_ok_and_marks_failed(caplog):
    _seed_active("sched_c")
    # Prompt mentions BigQuery via dotted-table; fire only called
    # sheets_read (not in BQ group). Must flip to Failed.
    runner = _runner_with_calls(["sheets_read"])
    prompt = "Query `reports.business_report_asin` for the top 50."
    with caplog.at_level("ERROR"):
        with patch.object(tasks, "_log_job_event") as mock_log:
            with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()) as mock_alert:
                out_resp, out_ok = await _check_fabrication_for_scheduled_fire(
                    runner=runner, user_id="u", session_id="sess_c",
                    task_id="sched_c", owner_user_id="sergey@mellanni.com",
                    task_prompt=prompt, response="Found 56 discrepancies.",
                    start_epoch=1000.0,
                )
    assert out_ok is False
    assert "fabrication detected" in ACTIVE_TASKS["sched_c"]["status"].lower()
    assert ACTIVE_TASKS["sched_c"]["status"].startswith("Failed (")
    assert "fabricated" in out_resp.lower()
    assert "Found 56 discrepancies." in out_resp
    assert ACTIVE_TASKS["sched_c"]["error"] == "fabrication_detected_required_group"
    # _log_job_event called with an "error" event.
    assert mock_log.called
    call_args = mock_log.call_args
    assert call_args.args[0] == "error"
    assert call_args.kwargs.get("task_id") == "sched_c"
    assert "fabrication_detected" in call_args.kwargs.get("error", "")
    # notify_admins called.
    assert mock_alert.called
    alert_text = mock_alert.call_args.args[0]
    assert "sched_c" in alert_text and "sess_c" in alert_text
    # Severity = ERROR (not WARNING).
    assert any(
        rec.levelname == "ERROR" and "sched_c" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_required_any_not_satisfied_severity_is_error_not_warning(caplog):
    _seed_active("sched_d")
    runner = _runner_with_calls([])
    prompt = "Query `reports.business_report_asin`."
    with caplog.at_level("WARNING"):
        with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()):
            await _check_fabrication_for_scheduled_fire(
                runner=runner, user_id="u", session_id="s",
                task_id="sched_d", owner_user_id="o",
                task_prompt=prompt, response="X",
                start_epoch=1000.0,
            )
    # Required-group path uses logger.error; no WARNING record for
    # this fabrication path. (The list_invocation_tool_calls helper
    # may emit its own WARNING for empty events — that's allowed.)
    fab_records = [
        rec for rec in caplog.records
        if "fabrication" in rec.message.lower()
        or "required tool group not invoked" in rec.message.lower()
    ]
    assert fab_records, "no fabrication record at all"
    assert all(rec.levelname == "ERROR" for rec in fab_records), (
        f"expected only ERROR for required-group; got "
        f"{[(r.levelname, r.message) for r in fab_records]}"
    )


# ---------------------------------------------------------------------------
# Fail-the-task: un-runnable trigger (Drive upload)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drive_upload_unrunnable_uses_critical_severity(caplog):
    _seed_active("sched_e")
    # Fire called drive_list_files (read-only) — does NOT satisfy
    # "upload to Drive". Must still flip to Failed, severity CRITICAL.
    runner = _runner_with_calls(["drive_list_files"])
    prompt = "Generate a CSV and upload it to Google Drive."
    with caplog.at_level("CRITICAL"):
        with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()):
            out_resp, out_ok = await _check_fabrication_for_scheduled_fire(
                runner=runner, user_id="u", session_id="s",
                task_id="sched_e", owner_user_id="o",
                task_prompt=prompt, response="Uploaded.",
                start_epoch=1000.0,
            )
    assert out_ok is False
    assert ACTIVE_TASKS["sched_e"]["error"] == "fabrication_detected_unrunnable"
    assert "fabricated" in out_resp.lower()
    # CRITICAL log fired for the un-runnable branch.
    assert any(
        rec.levelname == "CRITICAL" and "ghost-tool" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Scan failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_scan_failure_marks_failed_and_alerts(caplog):
    _seed_active("sched_f")
    # Helper's get_session raises -> list_invocation_tool_calls re-raises.
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    runner.session_service.get_session.side_effect = RuntimeError("DB down")
    with caplog.at_level("CRITICAL"):
        with patch.object(tasks, "_log_job_event") as mock_log:
            with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()) as mock_alert:
                out_resp, out_ok = await _check_fabrication_for_scheduled_fire(
                    runner=runner, user_id="u", session_id="sess_f",
                    task_id="sched_f", owner_user_id="o",
                    task_prompt="Query `reports.business_report_asin`",
                    response="Done.", start_epoch=1000.0,
                )
    assert out_ok is False
    assert ACTIVE_TASKS["sched_f"]["status"] == "Failed (event scan failed)"
    assert "cannot self-verify" in out_resp
    # _log_job_event called with "error".
    assert mock_log.called
    assert mock_log.call_args.args[0] == "error"
    assert mock_log.call_args.kwargs.get("error") == "fabrication scan failed"
    # Admin alert called.
    assert mock_alert.called
    # CRITICAL came from list_invocation_tool_calls.
    assert any(
        rec.levelname == "CRITICAL"
        and "list_invocation_tool_calls" in rec.message
        and "sess_f" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Rule 13 cascade — admin-alert delivery failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_admins_safe_swallows_alert_exception(caplog):
    """Admin-alert failure must NOT propagate AND must log CRITICAL."""
    with patch("app.contracts.admin_alert.notify_admins",
               new=AsyncMock(side_effect=RuntimeError("Slack down"))):
        with caplog.at_level("CRITICAL"):
            await _notify_admins_safe("hello", task_id="sched_g")
    # Did not raise; CRITICAL logged.
    assert any(
        rec.levelname == "CRITICAL"
        and "sched_g" in rec.message
        and "admin-alert" in rec.message.lower()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_notify_admins_safe_happy_path():
    """Successful admin alert leaves no log + does not raise."""
    mock_notify = AsyncMock(return_value={"delivered": 1, "attempted": 1, "admins": []})
    with patch("app.contracts.admin_alert.notify_admins", new=mock_notify):
        await _notify_admins_safe("hello", task_id="sched_h")
    assert mock_notify.called
    assert mock_notify.call_args.args[0] == "hello"


# ---------------------------------------------------------------------------
# Multi-trigger composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_satisfaction_flips_failed_when_any_group_unsatisfied():
    """The cron_97f22322 prompt trips BigQuery + CSV + Drive-upload.
    Fire calls execute_sql + data_to_csv (satisfies BQ + CSV) but does
    nothing for the un-runnable Drive-upload — still must fail."""
    _seed_active("sched_i")
    runner = _runner_with_calls(["execute_sql", "data_to_csv"])
    prompt = (
        "Query `reports.business_report_asin` for top 50, then create "
        "a CSV report, then upload the CSV to Google Drive."
    )
    with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()):
        out_resp, out_ok = await _check_fabrication_for_scheduled_fire(
            runner=runner, user_id="u", session_id="s",
            task_id="sched_i", owner_user_id="o",
            task_prompt=prompt, response="Uploaded.",
            start_epoch=1000.0,
        )
    assert out_ok is False
    # Un-runnable wins the error classification (it can never be
    # satisfied; required-group path could legitimately succeed next
    # fire).
    assert ACTIVE_TASKS["sched_i"]["error"] == "fabrication_detected_unrunnable"


@pytest.mark.asyncio
async def test_all_groups_satisfied_with_unrunnable_still_fails():
    """Even when every required_any group has a call, the un-runnable
    Drive-upload trigger alone forces failure (there is no satisfying
    tool ever)."""
    _seed_active("sched_j")
    runner = _runner_with_calls([
        "execute_sql", "data_to_csv", "drive_list_files",
    ])
    prompt = (
        "Query `reports.business_report_asin`, create a CSV report, "
        "upload the CSV to Google Drive."
    )
    with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()):
        out_resp, out_ok = await _check_fabrication_for_scheduled_fire(
            runner=runner, user_id="u", session_id="s",
            task_id="sched_j", owner_user_id="o",
            task_prompt=prompt, response="Uploaded.",
            start_epoch=1000.0,
        )
    assert out_ok is False
    assert ACTIVE_TASKS["sched_j"]["error"] == "fabrication_detected_unrunnable"


@pytest.mark.asyncio
async def test_response_carries_actually_called_list():
    """User-facing prefix must include the list of tools actually
    called so the operator can diagnose. Real cron_97f22322 case:
    bigquery group unsatisfied; fire called nothing."""
    _seed_active("sched_k")
    runner = _runner_with_calls([])  # zero tool calls
    prompt = "Query `reports.business_report_asin`."
    with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()):
        out_resp, _ = await _check_fabrication_for_scheduled_fire(
            runner=runner, user_id="u", session_id="s",
            task_id="sched_k", owner_user_id="o",
            task_prompt=prompt, response="56 discrepancies found.",
            start_epoch=1000.0,
        )
    assert "Tools actually called: []" in out_resp
    assert "execute_sql" in out_resp  # required group listed in details
    assert "56 discrepancies found." in out_resp  # original response preserved
