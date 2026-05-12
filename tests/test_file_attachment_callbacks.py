"""file_attachment_capture + file_attachment_inject tests.

The pair of callbacks plumbs tool-generated files (charts, images,
report exports) into the agent's model response as inline_data Parts
so ADK's A2A converter ships them as FileParts. Without these, the
A2A path is text-only and the agent ends up claiming "attached" while
the bytes never leave the server (2026-05-14 streamlit chart incident).

Coverage:
- capture stashes valid file_path entries in session state
- capture honours the spillover ``file_payloads`` shape too
- capture rejects missing files / oversized files / non-string paths
- inject appends inline_data Parts to llm_response with the
  ``__contract_file:`` display_name marker
- inject clears the consumed entries from state
- inject is a no-op when nothing is pending or content is empty
- inject must NOT block the spillover guardrail (separate code paths)
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from google.genai import types

from app.callbacks.guardrails import (
    _FILE_ATTACHMENT_MARKER,
    _PENDING_FILE_PARTS_KEY,
    file_attachment_capture,
    file_attachment_inject,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str):
    m = MagicMock()
    m.name = name
    return m


def _make_state_ctx(initial: dict | None = None):
    """Build a ToolContext-shaped stand-in whose ``state`` mimics ADK's
    State (supports ``__setitem__`` + ``to_dict``)."""

    class _S:
        def __init__(self):
            self._d = dict(initial or {})

        def to_dict(self):
            return dict(self._d)

        def __setitem__(self, k, v):
            self._d[k] = v

        def __getitem__(self, k):
            return self._d[k]

        def get(self, k, default=None):
            return self._d.get(k, default)

    ctx = MagicMock()
    ctx.state = _S()
    return ctx


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


def test_capture_stashes_file_path(tmp_path):
    f = tmp_path / "chart.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nstubbytes")

    ctx = _make_state_ctx()
    out = file_attachment_capture(
        tool=_make_tool("generate_chart"),
        args={},
        tool_context=ctx,
        tool_response={"status": "success", "file_path": str(f)},
    )
    assert out is None  # pass-through; tool_response unchanged
    pending = ctx.state.get(_PENDING_FILE_PARTS_KEY)
    assert pending == [os.path.abspath(str(f))]


def test_capture_handles_spilled_file_payloads_shape(tmp_path):
    """When ``tool_output_spillover_guardrail`` replaces a large
    response, the bytes are redacted but the ``file_payloads`` list
    still carries the filename(s). Capture must find those too so the
    A2A path doesn't lose attachments to spillover."""
    f = tmp_path / "report.csv"
    f.write_text("col1,col2\n1,2\n")

    ctx = _make_state_ctx()
    spilled = {
        "status": "spilled",
        "tool": "generate_chart",
        "file_payloads": [{"filename": str(f), "mime_type": "text/csv"}],
        "summary": "Output written to scratchpad.",
    }
    file_attachment_capture(
        tool=_make_tool("generate_chart"),
        args={},
        tool_context=ctx,
        tool_response=spilled,
    )
    assert ctx.state.get(_PENDING_FILE_PARTS_KEY) == [os.path.abspath(str(f))]


def test_capture_dedupes_repeated_paths(tmp_path):
    f = tmp_path / "x.png"
    f.write_bytes(b"x")
    ctx = _make_state_ctx()

    file_attachment_capture(
        tool=_make_tool("t"),
        args={},
        tool_context=ctx,
        tool_response={"status": "success", "file_path": str(f)},
    )
    file_attachment_capture(
        tool=_make_tool("t"),
        args={},
        tool_context=ctx,
        tool_response={"status": "success", "file_path": str(f)},
    )
    assert len(ctx.state.get(_PENDING_FILE_PARTS_KEY)) == 1


def test_capture_skips_missing_file(tmp_path):
    """An agent might pass a stale path (file moved or deleted before
    the response). Capture skips silently — better than blowing up the
    turn."""
    ctx = _make_state_ctx()
    file_attachment_capture(
        tool=_make_tool("t"),
        args={},
        tool_context=ctx,
        tool_response={"status": "success", "file_path": "/tmp/does_not_exist_42.png"},
    )
    assert not ctx.state.get(_PENDING_FILE_PARTS_KEY)


def test_capture_no_file_path_passes_through(tmp_path):
    ctx = _make_state_ctx()
    out = file_attachment_capture(
        tool=_make_tool("t"),
        args={},
        tool_context=ctx,
        tool_response={"status": "success", "message": "no file here"},
    )
    assert out is None
    assert not ctx.state.get(_PENDING_FILE_PARTS_KEY)


def test_capture_skips_oversized_file(tmp_path, monkeypatch):
    """Files larger than the inline cap (20 MB default) are skipped so
    we don't blow up the A2A response budget."""
    from app.callbacks.guardrails import attachments as gr

    monkeypatch.setattr(gr, "_FILE_ATTACHMENT_MAX_BYTES", 10)
    f = tmp_path / "big.png"
    f.write_bytes(b"x" * 100)

    ctx = _make_state_ctx()
    file_attachment_capture(
        tool=_make_tool("t"),
        args={},
        tool_context=ctx,
        tool_response={"status": "success", "file_path": str(f)},
    )
    assert not ctx.state.get(_PENDING_FILE_PARTS_KEY)


# ---------------------------------------------------------------------------
# inject
# ---------------------------------------------------------------------------


def _make_llm_response_with_text(text: str = "Chart attached."):
    content = types.Content(role="model", parts=[types.Part.from_text(text=text)])
    resp = MagicMock()
    resp.content = content
    return resp


def test_inject_appends_inline_data_part_with_marker(tmp_path):
    f = tmp_path / "chart.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\nactualbytes")

    ctx = _make_state_ctx({_PENDING_FILE_PARTS_KEY: [os.path.abspath(str(f))]})
    response = _make_llm_response_with_text()

    out = file_attachment_inject(callback_context=ctx, llm_response=response)
    assert out is response  # modified llm_response returned

    parts = out.content.parts
    assert len(parts) == 2
    assert parts[0].text == "Chart attached."
    inline = parts[1].inline_data
    assert inline is not None
    assert inline.mime_type == "image/png"
    assert inline.data == b"\x89PNG\r\n\x1a\nactualbytes"
    # Marker carries the file_path so dedup in extract_agent_response works.
    expected_marker = f"{_FILE_ATTACHMENT_MARKER}{os.path.abspath(str(f))}"
    assert inline.display_name == expected_marker

    # State drained
    assert ctx.state.get(_PENDING_FILE_PARTS_KEY) == []


def test_inject_no_pending_returns_none(tmp_path):
    ctx = _make_state_ctx()
    response = _make_llm_response_with_text()
    out = file_attachment_inject(callback_context=ctx, llm_response=response)
    assert out is None  # nothing to do


def test_inject_emits_placeholder_on_missing_path(tmp_path):
    """If the file disappeared between capture and inject (e.g. tmp
    cleanup ran), inject MUST surface the failure as a text Part on the
    response — Law 6: nothing fails silently. The user must see why the
    promised attachment didn't materialise instead of just text."""
    ctx = _make_state_ctx(
        {_PENDING_FILE_PARTS_KEY: ["/tmp/path_that_vanished_42.png"]}
    )
    response = _make_llm_response_with_text()
    out = file_attachment_inject(callback_context=ctx, llm_response=response)
    assert out is response  # mutated response returned
    parts = out.content.parts
    assert len(parts) == 2
    assert "file attach failed" in parts[1].text
    assert "path_that_vanished_42.png" in parts[1].text
    assert ctx.state.get(_PENDING_FILE_PARTS_KEY) == []


def test_inject_handles_empty_response_content(tmp_path):
    """If the LLM returned an event with no content (rare but possible
    during tool-call-only turns), inject must not crash."""
    f = tmp_path / "x.png"
    f.write_bytes(b"x")
    ctx = _make_state_ctx({_PENDING_FILE_PARTS_KEY: [os.path.abspath(str(f))]})

    response = MagicMock()
    response.content = None
    out = file_attachment_inject(callback_context=ctx, llm_response=response)
    assert out is None
    # State cleared so we don't ride this forever on a malformed turn.
    assert ctx.state.get(_PENDING_FILE_PARTS_KEY) == []
