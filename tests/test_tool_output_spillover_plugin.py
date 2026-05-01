"""Unit tests for ToolOutputSpilloverPlugin."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.plugins.tool_output_spillover import ToolOutputSpilloverPlugin


def _fake_tool(name="bigquery_query"):
    return SimpleNamespace(name=name)


@pytest.fixture
def plugin():
    return ToolOutputSpilloverPlugin()


@pytest.fixture
def small_threshold(monkeypatch):
    monkeypatch.setenv("TOOL_OUTPUT_SPILL_THRESHOLD", "100")


@pytest.fixture
def fake_scratchpad():
    with patch("app.tools.scratchpad.scratchpad_write") as m:
        m.return_value = {"status": "success", "scratchpad": "test", "size_bytes": 0}
        yield m


@pytest.mark.asyncio
async def test_under_threshold_passes_through(plugin, small_threshold, fake_scratchpad):
    result = await plugin.after_tool_callback(
        tool=_fake_tool(),
        tool_args={},
        tool_context=MagicMock(),
        result={"data": "small"},
    )
    assert result is None
    fake_scratchpad.assert_not_called()


@pytest.mark.asyncio
async def test_over_threshold_spills(plugin, small_threshold, fake_scratchpad):
    big = {"rows": ["row " + str(i) for i in range(50)]}
    result = await plugin.after_tool_callback(
        tool=_fake_tool(),
        tool_args={},
        tool_context=MagicMock(),
        result=big,
    )
    assert result is not None
    assert result["status"] == "spilled"
    assert result["tool"] == "bigquery_query"
    assert result["scratchpad_name"].startswith("_spill_bigquery_query_")
    assert "scratchpad_read" in result["summary"]
    fake_scratchpad.assert_called_once()


@pytest.mark.asyncio
async def test_string_response_spills(plugin, small_threshold, fake_scratchpad):
    result = await plugin.after_tool_callback(
        tool=_fake_tool(name="web_fetch"),
        tool_args={},
        tool_context=MagicMock(),
        result="x" * 200,
    )
    assert result is not None
    assert result["size_chars"] == 200


@pytest.mark.asyncio
async def test_exempt_tools_never_spill(plugin, small_threshold, fake_scratchpad):
    huge = {"content": "x" * 10000}
    for name in ("scratchpad_read", "scratchpad_write", "scratchpad_list"):
        result = await plugin.after_tool_callback(
            tool=_fake_tool(name=name),
            tool_args={},
            tool_context=MagicMock(),
            result=huge,
        )
        assert result is None, f"{name} should be exempt"
    fake_scratchpad.assert_not_called()


@pytest.mark.asyncio
async def test_none_response_passes_through(plugin, small_threshold, fake_scratchpad):
    result = await plugin.after_tool_callback(
        tool=_fake_tool(),
        tool_args={},
        tool_context=MagicMock(),
        result=None,
    )
    assert result is None
    fake_scratchpad.assert_not_called()


@pytest.mark.asyncio
async def test_default_threshold_8000(plugin, monkeypatch, fake_scratchpad):
    monkeypatch.delenv("TOOL_OUTPUT_SPILL_THRESHOLD", raising=False)
    under = await plugin.after_tool_callback(
        tool=_fake_tool(), tool_args={}, tool_context=MagicMock(), result="x" * 7000,
    )
    over = await plugin.after_tool_callback(
        tool=_fake_tool(), tool_args={}, tool_context=MagicMock(), result="x" * 9000,
    )
    assert under is None
    assert over is not None


@pytest.mark.asyncio
async def test_preview_truncated_500(plugin, small_threshold, fake_scratchpad):
    result = await plugin.after_tool_callback(
        tool=_fake_tool(),
        tool_args={},
        tool_context=MagicMock(),
        result="abcdefghij" * 200,
    )
    assert len(result["preview"]) <= 501
    assert result["preview"].endswith("…")


@pytest.mark.asyncio
async def test_scratchpad_failure_falls_through(plugin, small_threshold):
    """If scratchpad_write raises, fall through to original response."""
    with patch("app.tools.scratchpad.scratchpad_write", side_effect=OSError("disk full")):
        result = await plugin.after_tool_callback(
            tool=_fake_tool(),
            tool_args={},
            tool_context=MagicMock(),
            result="x" * 200,
        )
        assert result is None
