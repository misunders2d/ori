"""Tests for ``app.tasks._check_suspect_phrase_for_scheduled_fire``
(Fix 2.3 of the cron_97f22322 scheduling-fixes work).

The helper inspects the LLM's outgoing response for known
fabrication phrases (Slack post + monitor reply wording from the
2026-05-20 incident — neither phrase appears in source). On a hit
the three branches under test are:

  (a) Event scan fails  → CRITICAL log, admin alert, fail-the-task,
      "cannot self-verify" prefix.
  (b) Real tool error recorded AND paraphrased by the LLM → ERROR
      log, admin alert, fail-the-task, response prepended with
      verbatim error messages.
  (c) Suspect phrase + zero recorded tool errors → CRITICAL log
      (pure fabrication), admin alert, fail-the-task, "treat as
      fabricated" prefix.

Also pins:
  * No suspect phrase → response unchanged, ``agent_ok = True``.
  * Empty response → no-op (no fail-the-task false positive).
  * Each suspect phrase from the curated list independently triggers
    detection.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.tasks as tasks
from app.tasks import (
    ACTIVE_TASKS,
    _SUSPECT_PHRASES,
    _check_suspect_phrase_for_scheduled_fire,
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


def _mk_fr_event(ts: float, name: str, response: object) -> SimpleNamespace:
    part = SimpleNamespace(
        function_call=None,
        function_response=SimpleNamespace(name=name, response=response),
    )
    content = SimpleNamespace(parts=[part])
    return SimpleNamespace(timestamp=ts, content=content)


def _runner_with_events(events: list) -> MagicMock:
    session = MagicMock()
    session.events = events
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    runner.session_service.get_session.return_value = session
    return runner


# ---------------------------------------------------------------------------
# Pass-through paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_suspect_phrase_returns_unchanged():
    _seed_active("sched_a")
    runner = _runner_with_events([])
    out_resp, out_ok = await _check_suspect_phrase_for_scheduled_fire(
        runner=runner, user_id="u", session_id="s",
        task_id="sched_a", owner_user_id="o",
        response="All good. 56 discrepancies recorded.",
        start_epoch=1000.0,
    )
    assert out_resp == "All good. 56 discrepancies recorded."
    assert out_ok is True
    assert ACTIVE_TASKS["sched_a"]["status"] == "Completed"


@pytest.mark.asyncio
async def test_empty_response_no_op():
    _seed_active("sched_b")
    runner = _runner_with_events([])
    out_resp, out_ok = await _check_suspect_phrase_for_scheduled_fire(
        runner=runner, user_id="u", session_id="s",
        task_id="sched_b", owner_user_id="o",
        response="", start_epoch=1000.0,
    )
    assert out_resp == ""
    assert out_ok is True


# ---------------------------------------------------------------------------
# Each curated phrase triggers detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", _SUSPECT_PHRASES)
@pytest.mark.asyncio
async def test_each_curated_phrase_triggers_failure(phrase):
    _seed_active("sched_p")
    runner = _runner_with_events([])  # zero tool errors → pure-fabrication branch
    response = f"Something happened. {phrase}. Carry on."
    with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()):
        out_resp, out_ok = await _check_suspect_phrase_for_scheduled_fire(
            runner=runner, user_id="u", session_id="s",
            task_id="sched_p", owner_user_id="o",
            response=response, start_epoch=1000.0,
        )
    assert out_ok is False, f"phrase {phrase!r} did not trigger detection"
    assert ACTIVE_TASKS["sched_p"]["status"].startswith("Failed ")


# ---------------------------------------------------------------------------
# Branch (a): event scan failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_scan_failure_fails_task_and_alerts(caplog):
    _seed_active("sched_c")
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    runner.session_service.get_session.side_effect = RuntimeError("DB down")
    with caplog.at_level("CRITICAL"):
        with patch.object(tasks, "_log_job_event") as mock_log:
            with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()) as mock_alert:
                out_resp, out_ok = await _check_suspect_phrase_for_scheduled_fire(
                    runner=runner, user_id="u", session_id="sess_c",
                    task_id="sched_c", owner_user_id="o",
                    response="Drive upload was bypassed for the report.",
                    start_epoch=1000.0,
                )
    assert out_ok is False
    assert ACTIVE_TASKS["sched_c"]["status"] == "Failed (suspect-phrase scan failed)"
    assert ACTIVE_TASKS["sched_c"]["error"] == "suspect-phrase scan failed"
    assert "cannot self-verify" not in out_resp  # phrasing belongs to Fix 2.2 helper
    assert "event scan failed" in out_resp
    # _log_job_event called with "error".
    assert mock_log.called
    assert mock_log.call_args.args[0] == "error"
    assert mock_log.call_args.kwargs.get("error") == "suspect-phrase scan failed"
    assert mock_log.call_args.kwargs.get("session_id") == "sess_c"
    # Admin alert called.
    assert mock_alert.called
    alert_text = mock_alert.call_args.args[0]
    assert "sched_c" in alert_text and "sess_c" in alert_text
    # CRITICAL from list_invocation_tool_calls.
    assert any(
        rec.levelname == "CRITICAL"
        and "list_invocation_tool_calls" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Branch (b): real tool error AND LLM paraphrased
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_tool_error_paraphrased_prepends_verbatim_and_fails(caplog):
    _seed_active("sched_d")
    runner = _runner_with_events([
        _mk_fr_event(2000.0, "sheets_write",
                     {"status": "error", "message": "Sheets 403: forbidden"}),
        _mk_fr_event(2000.5, "sheets_write",
                     {"status": "error", "message": "Sheets 429: rate limit"}),
    ])
    with caplog.at_level("ERROR"):
        with patch.object(tasks, "_log_job_event") as mock_log:
            with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()) as mock_alert:
                out_resp, out_ok = await _check_suspect_phrase_for_scheduled_fire(
                    runner=runner, user_id="u", session_id="sess_d",
                    task_id="sched_d", owner_user_id="o",
                    response="Drive upload was bypassed; all good.",
                    start_epoch=1000.0,
                )
    assert out_ok is False
    assert ACTIVE_TASKS["sched_d"]["status"] == (
        "Failed (suspect phrase + real tool error paraphrased)"
    )
    assert ACTIVE_TASKS["sched_d"]["error"] == "suspect-phrase law6 paraphrase"
    # Verbatim errors prepended.
    assert "Sheets 403: forbidden" in out_resp
    assert "Sheets 429: rate limit" in out_resp
    # Original LLM response preserved below the "--- LLM's ..." marker.
    assert "Drive upload was bypassed; all good." in out_resp
    # _log_job_event called with tool_error_count.
    assert mock_log.called
    assert mock_log.call_args.kwargs.get("tool_error_count") == 2
    assert mock_log.call_args.kwargs.get("error") == "suspect-phrase law6 paraphrase"
    # Admin alert called.
    assert mock_alert.called
    # Severity: ERROR not CRITICAL.
    fab_records = [
        rec for rec in caplog.records
        if "suspect-phrase Law-6" in rec.message
    ]
    assert fab_records
    assert all(rec.levelname == "ERROR" for rec in fab_records), (
        f"expected ERROR; got {[(r.levelname, r.message) for r in fab_records]}"
    )


@pytest.mark.asyncio
async def test_success_status_tool_response_is_not_a_real_error():
    """A function_response with status=success must NOT classify as a
    real error — the LLM is then in the pure-fabrication branch."""
    _seed_active("sched_e")
    runner = _runner_with_events([
        _mk_fr_event(2000.0, "drive_list_files",
                     {"status": "success", "files": []}),
    ])
    with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()):
        out_resp, out_ok = await _check_suspect_phrase_for_scheduled_fire(
            runner=runner, user_id="u", session_id="s",
            task_id="sched_e", owner_user_id="o",
            response="permission/scope mismatch occurred.",
            start_epoch=1000.0,
        )
    assert out_ok is False
    # Pure-fabrication branch, NOT the paraphrase branch.
    assert ACTIVE_TASKS["sched_e"]["error"] == "suspect-phrase fabricated cause"


# ---------------------------------------------------------------------------
# Branch (c): pure fabrication (suspect phrase + zero real errors)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pure_fabrication_logs_critical_and_admin_alerts(caplog):
    _seed_active("sched_f")
    runner = _runner_with_events([])  # zero events at all
    with caplog.at_level("CRITICAL"):
        with patch.object(tasks, "_log_job_event") as mock_log:
            with patch.object(tasks, "_notify_admins_safe", new=AsyncMock()) as mock_alert:
                out_resp, out_ok = await _check_suspect_phrase_for_scheduled_fire(
                    runner=runner, user_id="u", session_id="sess_f",
                    task_id="sched_f", owner_user_id="o",
                    response=(
                        "Report done. Note: Google Drive upload was "
                        "bypassed as the account is not connected. The "
                        "CSV is attached."
                    ),
                    start_epoch=1000.0,
                )
    assert out_ok is False
    assert ACTIVE_TASKS["sched_f"]["status"] == (
        "Failed (suspect phrase + no recorded tool error: fabricated)"
    )
    assert ACTIVE_TASKS["sched_f"]["error"] == "suspect-phrase fabricated cause"
    # User-facing prefix.
    assert "Treat as fabricated" in out_resp
    # Original response preserved.
    assert "Note: Google Drive upload was bypassed" in out_resp
    # _log_job_event called with "error".
    assert mock_log.called
    assert mock_log.call_args.args[0] == "error"
    assert mock_log.call_args.kwargs.get("error") == "suspect-phrase fabricated cause"
    assert mock_log.call_args.kwargs.get("session_id") == "sess_f"
    # Admin alert called.
    assert mock_alert.called
    # Severity = CRITICAL.
    assert any(
        rec.levelname == "CRITICAL"
        and "Law 6 violation — fabricated cause" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Rule 13 cascade — admin alert failure path doesn't propagate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_alert_failure_during_detection_does_not_raise(caplog):
    _seed_active("sched_g")
    runner = _runner_with_events([])
    # Force notify_admins to raise; _notify_admins_safe should swallow.
    with patch("app.contracts.admin_alert.notify_admins",
               new=AsyncMock(side_effect=RuntimeError("Slack down"))):
        with caplog.at_level("CRITICAL"):
            out_resp, out_ok = await _check_suspect_phrase_for_scheduled_fire(
                runner=runner, user_id="u", session_id="s",
                task_id="sched_g", owner_user_id="o",
                response="permission/scope mismatch",
                start_epoch=1000.0,
            )
    # Detection still ran, task still failed, no propagation.
    assert out_ok is False
    assert ACTIVE_TASKS["sched_g"]["error"] == "suspect-phrase fabricated cause"
    # Cascade log present.
    assert any(
        rec.levelname == "CRITICAL"
        and "admin-alert" in rec.message.lower()
        and "sched_g" in rec.message
        for rec in caplog.records
    )
