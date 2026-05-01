"""ToolOutputSpilloverPlugin — auto-spill oversized tool outputs to scratchpad.

Universal cap on tool-output size injected into LLM context. Prevents the
session-bloat → context-limit → cascading-retry → quota-exhaust failure
mode (documented incident: 2026-04-30 FBA-discrepancy scheduled task on
the amazon_manager evolution returned a BigQuery rowset >1M tokens; plan
loop ran 25 times against the poisoned session).

How it works:
- After every tool call, check tool_response size.
- If >TOOL_OUTPUT_SPILL_THRESHOLD chars (default 8000, env-configurable):
  - Full output is written to a scratchpad named _spill_<tool>_<hash>.
  - LLM sees only a lightweight reference: status, summary, scratchpad_name,
    size_chars, preview (first 500 chars for smell-test).
  - Agent calls scratchpad_read(name) when it actually needs the full data.

scratchpad_* tools are exempt to avoid recursion.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from google.adk.plugins import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


_SPILL_EXEMPT_TOOLS = frozenset({
    "scratchpad_write",
    "scratchpad_read",
    "scratchpad_replace",
    "scratchpad_clear",
    "scratchpad_list",
})


def _tool_response_size(tool_response: Any) -> tuple[int, str]:
    """Return (size_chars, text_repr) for a tool response. None/empty → (0, '')."""
    if tool_response is None:
        return 0, ""
    if isinstance(tool_response, str):
        return len(tool_response), tool_response
    if isinstance(tool_response, dict):
        try:
            text = json.dumps(tool_response, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(tool_response)
        return len(text), text
    text = str(tool_response)
    return len(text), text


class ToolOutputSpilloverPlugin(BasePlugin):
    """Spill oversized tool outputs to scratchpad before they reach the LLM."""

    def __init__(self) -> None:
        super().__init__(name="tool_output_spillover")

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> dict | None:
        if tool.name in _SPILL_EXEMPT_TOOLS:
            return None

        threshold = int(os.environ.get("TOOL_OUTPUT_SPILL_THRESHOLD", "8000"))
        size, text = _tool_response_size(result)
        if size <= threshold:
            return None  # under budget — pass through

        scratchpad_name = f"_spill_{tool.name or 'tool'}_{uuid.uuid4().hex[:6]}"

        try:
            from app.tools.scratchpad import scratchpad_write
            scratchpad_write(scratchpad_name, text, tool_context=tool_context)
        except Exception as e:
            logger.warning(
                "tool_output_spillover: failed to write scratchpad %s for tool %s: %s",
                scratchpad_name, tool.name, e,
            )
            # Spillover failed — fall back to letting the original response
            # through. Better to risk context blow-up than to silently drop
            # the data the agent might need.
            return None

        logger.info(
            "tool_output_spillover: %s returned %d chars → spilled to %s",
            tool.name, size, scratchpad_name,
        )

        preview = text[:500]
        if len(text) > 500:
            preview += "…"

        return {
            "status": "spilled",
            "tool": tool.name,
            "scratchpad_name": scratchpad_name,
            "summary": (
                f"Tool '{tool.name}' returned {size:,} chars (~{size // 4:,} tokens). "
                f"Output written to scratchpad. Call scratchpad_read('{scratchpad_name}') "
                f"to load the full content if you need it."
            ),
            "size_chars": size,
            "size_tokens_estimate": size // 4,
            "preview": preview,
        }
