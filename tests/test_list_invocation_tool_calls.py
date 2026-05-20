"""Tests for ``app.core.agent_executor.list_invocation_tool_calls``.

The helper is the read-only event-scan primitive used by the
fabrication detector in ``app.tasks.run_scheduled_task``. Behaviour
contract under test:

* float-epoch ``since_ts`` filtering — ADK Events store float
  ``time.time()`` timestamps; comparing to a ``datetime`` would raise
  ``TypeError`` on every event.
* function_call extraction with ``args_keys``.
* function_response extraction with ``response_status`` /
  ``response_message`` when the payload is a ``dict``.
* events older than ``since_epoch`` are skipped.
* events with no content / no parts / non-numeric timestamp are
  skipped cleanly (no exception).
* empty session OR no events → returns ``[]`` AND logs WARNING (no
  silent empty result — Law 6).
* session-load failure → logs CRITICAL AND re-raises (no swallow).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.agent_executor import list_invocation_tool_calls


def _mk_fc_event(ts: float, name: str, args: dict | None = None) -> SimpleNamespace:
    """Build a synthetic ADK Event whose only Part is a function_call."""
    part = SimpleNamespace(
        function_call=SimpleNamespace(name=name, args=args or {}),
        function_response=None,
    )
    content = SimpleNamespace(parts=[part])
    return SimpleNamespace(timestamp=ts, content=content)


def _mk_fr_event(ts: float, name: str, response: object) -> SimpleNamespace:
    part = SimpleNamespace(
        function_call=None,
        function_response=SimpleNamespace(name=name, response=response),
    )
    content = SimpleNamespace(parts=[part])
    return SimpleNamespace(timestamp=ts, content=content)


def _mk_text_event(ts: float) -> SimpleNamespace:
    """Synthetic ADK Event with a text-only Part (no function call/response)."""
    part = SimpleNamespace(function_call=None, function_response=None)
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


@pytest.mark.asyncio
async def test_function_call_extracted_with_args_keys():
    runner = _runner_with_events([
        _mk_fc_event(1000.0, "execute_sql", {"sql": "SELECT 1", "params": {}}),
    ])
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=999.0)
    assert len(out) == 1
    assert out[0]["name"] == "execute_sql"
    assert sorted(out[0]["args_keys"]) == ["params", "sql"]
    assert out[0]["response_status"] is None
    assert out[0]["response_message"] is None
    assert out[0]["timestamp"] == 1000.0


@pytest.mark.asyncio
async def test_function_response_extracted_with_status_and_message():
    runner = _runner_with_events([
        _mk_fr_event(
            1500.0,
            "sheets_write",
            {"status": "error", "message": "Sheets 403: forbidden"},
        ),
    ])
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=1000.0)
    assert len(out) == 1
    assert out[0]["name"] == "sheets_write"
    assert out[0]["response_status"] == "error"
    assert out[0]["response_message"] == "Sheets 403: forbidden"


@pytest.mark.asyncio
async def test_function_response_non_dict_payload_yields_no_status_message():
    runner = _runner_with_events([
        _mk_fr_event(1500.0, "memory_search", ["row1", "row2"]),  # list, not dict
    ])
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=0.0)
    assert len(out) == 1
    assert out[0]["name"] == "memory_search"
    assert out[0]["response_status"] is None
    assert out[0]["response_message"] is None


@pytest.mark.asyncio
async def test_function_response_non_string_message_yields_none():
    runner = _runner_with_events([
        _mk_fr_event(1500.0, "x", {"status": "error", "message": {"nested": True}}),
    ])
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=0.0)
    assert out[0]["response_status"] == "error"
    assert out[0]["response_message"] is None  # non-string message dropped


@pytest.mark.asyncio
async def test_events_before_since_epoch_skipped():
    runner = _runner_with_events([
        _mk_fc_event(500.0, "early_tool", {}),  # before cutoff
        _mk_fc_event(1500.0, "recent_tool", {}),  # after cutoff
    ])
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=1000.0)
    names = [c["name"] for c in out]
    assert names == ["recent_tool"]


@pytest.mark.asyncio
async def test_float_epoch_does_not_raise_typeerror():
    """Regression: v4 used datetime for since_ts vs float event.timestamp →
    TypeError. v5+ contract is float-on-both-sides."""
    runner = _runner_with_events([
        _mk_fc_event(1000.5, "execute_sql", {}),
    ])
    # Should NOT raise. Returns the event because 1000.5 >= 999.0.
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=999.0)
    assert len(out) == 1


@pytest.mark.asyncio
async def test_text_only_event_skipped():
    """Events with no function_call AND no function_response yield no rows."""
    runner = _runner_with_events([
        _mk_text_event(1500.0),
    ])
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=0.0)
    assert out == []


@pytest.mark.asyncio
async def test_event_missing_timestamp_skipped():
    bad = SimpleNamespace(timestamp=None, content=None)
    runner = _runner_with_events([bad])
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=0.0)
    assert out == []


@pytest.mark.asyncio
async def test_event_non_numeric_timestamp_skipped():
    bad = SimpleNamespace(timestamp="not a number", content=None)
    runner = _runner_with_events([bad])
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=0.0)
    assert out == []


@pytest.mark.asyncio
async def test_event_no_content_skipped():
    bad = SimpleNamespace(timestamp=1500.0, content=None)
    runner = _runner_with_events([bad])
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=0.0)
    assert out == []


@pytest.mark.asyncio
async def test_event_no_parts_skipped():
    content = SimpleNamespace(parts=None)
    bad = SimpleNamespace(timestamp=1500.0, content=content)
    runner = _runner_with_events([bad])
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=0.0)
    assert out == []


@pytest.mark.asyncio
async def test_empty_session_returns_empty_and_logs_warning(caplog):
    runner = _runner_with_events([])
    with caplog.at_level("WARNING"):
        out = await list_invocation_tool_calls(runner, "u", "sess_xyz", since_epoch=0.0)
    assert out == []
    assert any(
        "has no events" in rec.message and "sess_xyz" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_session_none_returns_empty_and_logs_warning(caplog):
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    runner.session_service.get_session.return_value = None
    with caplog.at_level("WARNING"):
        out = await list_invocation_tool_calls(runner, "u", "sess_abc", since_epoch=0.0)
    assert out == []
    assert any(
        "has no events" in rec.message and "sess_abc" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_session_load_failure_raises_and_logs_critical(caplog):
    runner = MagicMock()
    runner.app_name = "ori"
    runner.session_service = AsyncMock()
    runner.session_service.get_session.side_effect = RuntimeError("DB down")
    with caplog.at_level("CRITICAL"):
        with pytest.raises(RuntimeError, match="DB down"):
            await list_invocation_tool_calls(
                runner, "u", "sess_broken", since_epoch=0.0,
            )
    # CRITICAL log fired with session id named so an operator can
    # pinpoint which session failed.
    assert any(
        "list_invocation_tool_calls" in rec.message
        and "sess_broken" in rec.message
        and rec.levelname == "CRITICAL"
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_mixed_call_and_response_in_same_session_both_returned():
    runner = _runner_with_events([
        _mk_fc_event(1000.0, "execute_sql", {"sql": "..."}),
        _mk_fr_event(1001.0, "execute_sql", {"status": "success", "rows": []}),
        _mk_fc_event(1002.0, "sheets_write", {"range": "A1"}),
        _mk_fr_event(1003.0, "sheets_write", {"status": "success"}),
    ])
    out = await list_invocation_tool_calls(runner, "u", "s", since_epoch=0.0)
    assert len(out) == 4
    # Order preserved by event order.
    assert out[0]["name"] == "execute_sql" and out[0]["response_status"] is None
    assert out[1]["name"] == "execute_sql" and out[1]["response_status"] == "success"
    assert out[2]["name"] == "sheets_write" and out[2]["response_status"] is None
    assert out[3]["name"] == "sheets_write" and out[3]["response_status"] == "success"
