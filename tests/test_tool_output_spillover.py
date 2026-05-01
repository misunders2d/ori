"""Unit tests for tool_output_spillover_guardrail.

Prevents oversized tool outputs from blowing past the LLM's context window.
The callback writes the full output to a scratchpad and returns a lightweight
reference, so the agent only sees a summary + preview unless it explicitly
calls scratchpad_read(name).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.callbacks.guardrails import tool_output_spillover_guardrail


class _FakeTool:
    def __init__(self, name="bigquery_query"):
        self.name = name


@pytest.fixture
def small_threshold(monkeypatch):
    """Force a small threshold so tests don't need 8KB of fixture data."""
    monkeypatch.setenv("TOOL_OUTPUT_SPILL_THRESHOLD", "100")


@pytest.fixture
def fake_scratchpad():
    """Patch scratchpad_write to capture calls without hitting disk."""
    with patch("app.tools.scratchpad.scratchpad_write") as m:
        m.return_value = {"status": "success", "scratchpad": "test", "size_bytes": 0}
        yield m


def test_under_threshold_passes_through(small_threshold, fake_scratchpad):
    tool = _FakeTool()
    response = {"data": "small"}
    result = tool_output_spillover_guardrail(tool, {}, MagicMock(), response)
    assert result is None  # pass-through
    fake_scratchpad.assert_not_called()


def test_over_threshold_spills_to_scratchpad(small_threshold, fake_scratchpad):
    tool = _FakeTool()
    big_response = {"rows": ["row " + str(i) for i in range(50)]}
    result = tool_output_spillover_guardrail(tool, {}, MagicMock(), big_response)

    assert result is not None
    assert result["status"] == "spilled"
    assert result["tool"] == "bigquery_query"
    assert result["scratchpad_name"].startswith("_spill_bigquery_query_")
    assert "scratchpad_read" in result["summary"]
    assert "size_chars" in result
    assert "preview" in result
    fake_scratchpad.assert_called_once()


def test_string_response_spills(small_threshold, fake_scratchpad):
    tool = _FakeTool(name="web_fetch")
    big_string = "x" * 200
    result = tool_output_spillover_guardrail(tool, {}, MagicMock(), big_string)
    assert result is not None
    assert result["status"] == "spilled"
    assert result["size_chars"] == 200


def test_exempt_tools_never_spill(small_threshold, fake_scratchpad):
    """scratchpad_* tools are exempt to avoid recursion."""
    huge_response = {"content": "x" * 10000}
    for tool_name in ("scratchpad_read", "scratchpad_write", "scratchpad_list"):
        tool = _FakeTool(name=tool_name)
        result = tool_output_spillover_guardrail(tool, {}, MagicMock(), huge_response)
        assert result is None, f"{tool_name} should be exempt"
    fake_scratchpad.assert_not_called()


def test_none_response_passes_through(small_threshold, fake_scratchpad):
    result = tool_output_spillover_guardrail(_FakeTool(), {}, MagicMock(), None)
    assert result is None
    fake_scratchpad.assert_not_called()


def test_empty_response_passes_through(small_threshold, fake_scratchpad):
    result = tool_output_spillover_guardrail(_FakeTool(), {}, MagicMock(), {})
    assert result is None
    fake_scratchpad.assert_not_called()


def test_default_threshold_8000(monkeypatch, fake_scratchpad):
    """Without env var override, threshold is 8000 chars."""
    monkeypatch.delenv("TOOL_OUTPUT_SPILL_THRESHOLD", raising=False)
    just_under = "x" * 7000
    just_over = "x" * 9000
    assert tool_output_spillover_guardrail(_FakeTool(), {}, MagicMock(), just_under) is None
    assert tool_output_spillover_guardrail(_FakeTool(), {}, MagicMock(), just_over) is not None


def test_preview_truncated_to_500(small_threshold, fake_scratchpad):
    tool = _FakeTool()
    big_string = "abcdefghij" * 200  # 2000 chars
    result = tool_output_spillover_guardrail(tool, {}, MagicMock(), big_string)
    assert len(result["preview"]) <= 501  # 500 + ellipsis
    assert result["preview"].endswith("…")


def test_scratchpad_failure_falls_through(small_threshold):
    """If scratchpad_write raises, fall through to original response (better
    to risk context blow-up than silently drop the data)."""
    with patch("app.tools.scratchpad.scratchpad_write", side_effect=OSError("disk full")):
        tool = _FakeTool()
        big_response = "x" * 200
        result = tool_output_spillover_guardrail(tool, {}, MagicMock(), big_response)
        assert result is None  # passed through despite size


def test_function_tool_name_resolution(small_threshold, fake_scratchpad):
    """When tool is a plain function (not a Tool object), use __name__."""
    def my_query():
        pass

    big_response = "x" * 200
    result = tool_output_spillover_guardrail(my_query, {}, MagicMock(), big_response)
    assert result is not None
    assert result["tool"] == "my_query"
