"""Pin the existing Law-6 surface on AmazonWorkspaceAgent.

The fabrication detector landing in this scheduling-fixes work
(slices 3 + 4) is a NEW Law-6 layer on top of an EXISTING one:
``surface_error_loudly_after_tool`` already wraps any tool that
returns ``{"status": "error", ...}`` with a hard ``[TOOL FAILURE —
RELAY VERBATIM ...]`` prefix + an explicit ``agent_directive``
field. The wrapper is the load-bearing defense when a tool DOES
return a real error; the fabrication detector handles the case
where the tool was never called.

This test file pins the existing wrapper so a future PR can't
quietly drop it from ``amazon_workspace_agent``'s
``after_tool_callback`` list. The 2026-05-20 cron_97f22322
incident analysis showed the wrapper structurally cannot fire
on tool-skipping; the new detection layer complements it but
does NOT replace it.
"""

from unittest.mock import MagicMock

import pytest

from app.callbacks.guardrails.bouncer import (
    _TOOL_FAILURE_PREFIX,
    surface_error_loudly_after_tool,
)


def test_amazon_workspace_agent_has_surface_error_loudly_after_tool():
    """Import the agent and assert the wrapper is wired into
    after_tool_callback. Guards against accidental removal during
    refactors."""
    from app.sub_agents.amazon_workspace_agent import amazon_workspace_agent

    callbacks = amazon_workspace_agent.after_tool_callback or []
    # ADK accepts either a single callable OR a list; coerce to list.
    if callable(callbacks) and not isinstance(callbacks, list):
        callbacks = [callbacks]
    callback_names = [getattr(c, "__name__", repr(c)) for c in callbacks]
    assert "surface_error_loudly_after_tool" in callback_names, (
        f"after_tool_callback missing the Law-6 wrapper. "
        f"Current callbacks: {callback_names}"
    )


def test_wrapper_prepends_prefix_and_adds_agent_directive():
    """Verbatim contract: error message gets prefixed; agent_directive
    is set; original message text preserved AFTER the prefix."""
    tool = MagicMock()
    tool.name = "sheets_write"
    args = {"spreadsheet_id": "abc", "range": "A1", "values": [["x"]]}
    tool_context = MagicMock()
    tool_context.state = {}
    tool_response = {
        "status": "error",
        "message": "Sheets 403: forbidden (user has no edit access).",
    }

    wrapped = surface_error_loudly_after_tool(tool, args, tool_context, tool_response)

    assert wrapped is not None
    assert wrapped["status"] == "error"
    assert wrapped["message"].startswith(_TOOL_FAILURE_PREFIX)
    assert "Sheets 403: forbidden" in wrapped["message"]
    assert isinstance(wrapped.get("agent_directive"), str)
    assert len(wrapped["agent_directive"]) > 0
    # Original dict not mutated.
    assert tool_response["message"] == "Sheets 403: forbidden (user has no edit access)."


def test_wrapper_skips_non_dict_response():
    """A list / string response (some tools return plain text) does
    not have a ``status`` field and must not be wrapped."""
    tool = MagicMock()
    tool.name = "sheets_read"
    out = surface_error_loudly_after_tool(tool, {}, MagicMock(), ["row1", "row2"])
    assert out is None


def test_wrapper_skips_success_response():
    """Only ``status: error`` triggers the wrap; ``status: success``
    must pass through untouched."""
    tool = MagicMock()
    tool.name = "sheets_write"
    out = surface_error_loudly_after_tool(
        tool, {}, MagicMock(),
        {"status": "success", "updated_range": "Sheet1!A1:A1"},
    )
    assert out is None


def test_wrapper_skips_response_without_string_message():
    """If ``message`` is not a string, the wrapper bails — the
    formatter would otherwise produce a confusing concatenation."""
    tool = MagicMock()
    tool.name = "drive_list_files"
    out = surface_error_loudly_after_tool(
        tool, {}, MagicMock(),
        {"status": "error", "message": None},
    )
    assert out is None


def test_wrapper_does_not_double_wrap():
    """If the message already starts with the prefix (callback chain
    ran the response through us before), the wrapper does not stack
    a second prefix."""
    tool = MagicMock()
    tool.name = "sheets_write"
    already_wrapped = {
        "status": "error",
        "message": f"{_TOOL_FAILURE_PREFIX}Sheets 403: forbidden",
    }
    out = surface_error_loudly_after_tool(tool, {}, MagicMock(), already_wrapped)
    assert out is not None
    # Exactly one prefix occurrence — no nesting.
    assert out["message"].count(_TOOL_FAILURE_PREFIX) == 1
