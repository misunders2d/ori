"""Gemini-schema compatibility audit for every registered tool.

The agent runs Gemini (gemini-3-flash-preview is the default for several
sub-agents). Gemini's function-declaration schema dialect is a strict
subset of JSON Schema — in particular, **`additionalProperties` is not
supported** anywhere in the schema tree. ADK's sanitizer drops it at the
top level of a tool schema, but it does *not* descend into every nested
shape (e.g. `any_of[].items.additional_properties`), so a parameter typed
as `list[dict] | None` will silently produce a schema Gemini rejects:

    400 INVALID_ARGUMENT: Unknown name "additional_properties" at
    'tools[0].function_declarations[N].parameters.properties[K].value.any_of[0].items'

That exact 400 took the production bot down on 2026-05-11 (`create_plan`
exposed `step_constraints: list[dict] | None`). This test walks every
tool registered under the live agent tree, asks ADK for its function
declaration, serialises it the same way ADK does before sending to
Gemini, and fails loudly if `additional_properties` (or its camelCase
form) shows up anywhere — catching the same class of regression at
collection time instead of at request time.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest


def _collect_tool_declarations():
    """Resolve every leaf tool reachable from the root agent, return
    `(agent_name, tool_name, declaration_dict)` triples.

    Uses the same `_get_declaration()` path ADK uses to build the
    `FunctionDeclaration` for Gemini, so the schema we inspect is the
    one Gemini will receive.
    """
    from app.agent import root_agent
    from google.adk.agents.readonly_context import ReadonlyContext
    from google.adk.tools.base_toolset import BaseToolset

    class _DummyInvocation:
        def __init__(self):
            self.session = type("S", (), {"state": {}, "session_id": "schema-audit"})()
            self.agent = None
            self.user_id = "schema-audit"

        def __getattr__(self, name):
            return None

    ctx = ReadonlyContext(_DummyInvocation())

    seen_agents: set[str] = set()
    triples: list[tuple[str, str, dict]] = []

    async def walk(agent):
        name = getattr(agent, "name", "?")
        if name in seen_agents:
            return
        seen_agents.add(name)

        leaf_tools = []
        for t in getattr(agent, "tools", []) or []:
            if isinstance(t, BaseToolset):
                try:
                    resolved = await t.get_tools(ctx)
                    leaf_tools.extend(resolved)
                except Exception:
                    # Toolsets that fail to resolve under a dummy
                    # context (e.g. require a real session, MCP socket,
                    # network) cannot contribute to the Gemini schema
                    # at startup either — skip them quietly. Real
                    # production bugs in those will surface elsewhere.
                    continue
            else:
                leaf_tools.append(t)

        for tool in leaf_tools:
            tn = getattr(tool, "name", type(tool).__name__)
            try:
                decl = tool._get_declaration() if hasattr(tool, "_get_declaration") else None
            except Exception:
                continue
            if decl is None:
                continue
            try:
                d = decl.model_dump(exclude_none=True)
            except Exception:
                continue
            triples.append((name, tn, d))

        for sa in getattr(agent, "sub_agents", []) or []:
            await walk(sa)

    asyncio.run(walk(root_agent))
    return triples


def _find_banned_field(node, banned: tuple[str, ...], path: str = "") -> list[str]:
    """Recursively scan a schema dict/list for any of `banned` field names.

    Returns the list of JSON-pointer-ish paths where a banned field was
    found. Empty list = clean.
    """
    hits: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{path}.{k}" if path else k
            if k in banned:
                hits.append(here)
            hits.extend(_find_banned_field(v, banned, here))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            hits.extend(_find_banned_field(item, banned, f"{path}[{i}]"))
    return hits


@pytest.mark.skipif(
    not (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
    ),
    reason="Agent boot requires provider credentials in env",
)
def test_no_tool_has_gemini_banned_fields_in_schema():
    """Every tool reachable from root_agent must have a Gemini-compatible
    function-declaration schema.

    Gemini's banned-fields list (as of ADK 1.28):
      - additional_properties (snake) / additionalProperties (camel)
      - $defs / definitions (Gemini doesn't resolve refs — ADK
        dereferences upstream, so we only fail if either leaks through)

    A failure means a tool's parameter type produces a schema that
    Gemini's strict JSON-Schema subset can't parse. The fix is almost
    always to replace `list[dict]` / `dict[str, Any]` parameter types
    with an explicit shape (TypedDict, Pydantic model) or a JSON-string
    bridge — see `app/tools/planner.py:create_plan` for the workaround
    used after the 2026-05-11 incident.
    """
    triples = _collect_tool_declarations()
    assert triples, "No tools collected — agent tree empty? schema audit can't run"

    banned = ("additional_properties", "additionalProperties", "$defs", "definitions")
    failures: list[str] = []
    for agent_name, tool_name, decl in triples:
        hits = _find_banned_field(decl, banned)
        if hits:
            failures.append(
                f"[{agent_name}] tool {tool_name!r} schema has banned fields at: "
                + ", ".join(hits)
                + "\nSchema:\n"
                + json.dumps(decl, indent=2)[:2000]
            )

    if failures:
        raise AssertionError(
            "Tools with Gemini-incompatible function declarations:\n\n"
            + "\n\n".join(failures)
        )
