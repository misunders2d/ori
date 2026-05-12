"""Stuck-agent bouncer — universal escape hatch back to CoordinatorAgent.

The problem: when a leaf agent (AmazonAgent, AmazonWorkspaceAgent, …)
hallucinates a tool name (`analyze_data`, `generate_chart`) that isn't
in its toolset, ADK raises a verbose ValueError ("Tool 'X' not found.
Available tools: …"). The model receives that error, often retries the
same call, and the user sees the dump repeatedly. ROUTING FALLBACK
instructions are a soft prompt — the LLM does not always honour them
under stress (2026-05-12 chart incident).

This module deterministically forces a transfer back to
``CoordinatorAgent`` when the leaf is provably stuck:

1. ``on_tool_error_bouncer`` — registered as ``on_tool_error_callback``.
   Fires on every uncaught tool exception (including ADK's tool-not-
   found ValueError). Arms a session-state flag and returns a clean
   error response so the model isn't fed the ADK dump.

2. ``force_bounce_before_model`` — registered FIRST in
   ``before_model_callback``. Reads the flag; if set, returns a
   synthetic ``LlmResponse`` whose only Part is a function_call to
   ``transfer_to_agent(agent_name='CoordinatorAgent')``. ADK's existing
   transfer plumbing handles the rest. The LLM is never called.

3. ``reset_error_history_after_tool`` — registered in
   ``after_tool_callback``. Clears the consecutive-error counter on
   any successful tool response so a single transient failure doesn't
   poison the next turn.

Trigger conditions (deliberately conservative — single legit tool
errors must NOT bounce, the agent should be allowed to recover):

- Tool-not-found ValueError (string starts with ``Tool '…' not
  found``) → bounce immediately. This is always a hallucination.
- Same tool name returned an error 2+ turns in a row → bounce. The
  agent is in a retry loop.

Coordinator itself is the bounce TARGET, so it does NOT register the
bouncer — bouncing to yourself would deadlock.
"""

from __future__ import annotations

import logging

from google.adk.models.llm_response import LlmResponse
from google.genai import types

logger = logging.getLogger(__name__)


_FORCE_BOUNCE_KEY = "__force_bounce_to_coordinator__"
_ERROR_HIST_KEY = "__consecutive_tool_errors__"
_BOUNCE_TARGET = "CoordinatorAgent"
_MAX_REPEATED_ERRORS = 2


def _is_tool_not_found(error: Exception) -> bool:
    msg = str(error or "")
    return msg.startswith("Tool '") and " not found" in msg


def on_tool_error_bouncer(tool, args, tool_context, error):
    """ADK ``on_tool_error_callback``. Detects hallucinated tool calls
    and repeated-error loops; arms a force-bounce flag the next
    ``before_model`` callback drains.

    Returns a clean error dict so the LLM doesn't see ADK's verbose
    "Tool 'X' not found. Available tools: …" dump (which itself tends
    to trigger another hallucination — the LLM treats the available-
    tools list as a buffet).
    """
    tool_name = getattr(tool, "name", "<unknown>")
    not_found = _is_tool_not_found(error)

    # Track consecutive errors per tool name. Same tool failing twice
    # in a row → stuck loop.
    hist_raw = tool_context.state.get(_ERROR_HIST_KEY, {})
    if not isinstance(hist_raw, dict):
        hist_raw = {}
    count = int(hist_raw.get(tool_name, 0)) + 1
    hist_raw[tool_name] = count
    tool_context.state[_ERROR_HIST_KEY] = hist_raw

    should_bounce = not_found or count >= _MAX_REPEATED_ERRORS

    if should_bounce:
        tool_context.state[_FORCE_BOUNCE_KEY] = True
        reason = (
            f"tool '{tool_name}' not found (likely hallucination)"
            if not_found
            else f"tool '{tool_name}' failed {count} turns in a row"
        )
        logger.warning(
            "bouncer: arming force-bounce to %s — %s",
            _BOUNCE_TARGET,
            reason,
        )
        return {
            "status": "error",
            "message": (
                f"This agent cannot service this request ({reason}). "
                f"Transferring control to {_BOUNCE_TARGET}."
            ),
        }

    # Pass through normal ADK error handling (None → ADK re-raises and
    # the LLM sees the error).
    return None


def force_bounce_before_model(callback_context, llm_request):
    """Before-model callback. If a previous tool error armed the
    bounce flag, synthesise a transfer_to_agent function_call so ADK
    routes to ``CoordinatorAgent`` without consulting the LLM. The
    LLM has already proven it can't recover; involving it again would
    just burn tokens and risk another hallucination.

    Must run FIRST in the before_model chain — earlier guards (prompt
    injection, plan enforcer) would otherwise mutate or block the
    synthetic response.
    """
    state = getattr(callback_context, "state", None)
    if state is None or not state.get(_FORCE_BOUNCE_KEY):
        return None

    # Clear flag + counter so the destination agent (Coordinator)
    # starts with a fresh slate. If Coordinator also fails, the next
    # bounce will re-arm — but Coordinator itself isn't wired into the
    # bouncer (it's the target), so this is end-of-line.
    state[_FORCE_BOUNCE_KEY] = False
    state[_ERROR_HIST_KEY] = {}

    logger.info("bouncer: synthesising transfer_to_agent → %s", _BOUNCE_TARGET)
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    function_call=types.FunctionCall(
                        name="transfer_to_agent",
                        args={"agent_name": _BOUNCE_TARGET},
                    )
                )
            ],
        )
    )


def reset_error_history_after_tool(tool, args, tool_context, tool_response):
    """After-tool callback. Zero the consecutive-error counter on any
    successful tool response so a single transient failure doesn't
    poison subsequent turns. A "successful" response is one without a
    top-level ``status: error`` field.
    """
    if isinstance(tool_response, dict) and tool_response.get("status") == "error":
        return None  # Don't reset; let the error count continue.
    try:
        if tool_context.state.get(_ERROR_HIST_KEY):
            tool_context.state[_ERROR_HIST_KEY] = {}
    except Exception:
        pass
    return None
