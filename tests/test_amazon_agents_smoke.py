"""Smoke tests for the amazon_manager-migrated sub-agents.

Each agent must:
- Import without raising
- Construct as an LlmAgent (or be None if a credential-gated agent)
- Have all tool declarations build cleanly via FunctionTool._get_declaration()

Catches the same regression class that test_tool_declarations_build catches
for the canonical agents, but scoped to the new amazon agents wired up in
phase 4 of the migration.
"""

import pytest
from google.adk.tools.function_tool import FunctionTool

_ALWAYS_BUILT = [
    ("amazon_agent", "AmazonAgent"),
    ("amazon_data_analyst_agent", "AmazonDataAnalystAgent"),
    ("amazon_memory_agent", "AmazonMemoryAgent"),
    ("amazon_workspace_agent", "AmazonWorkspaceAgent"),
    ("amazon_head_agent", "AmazonHeadAgent"),
]


@pytest.mark.parametrize("attr,expected_name", _ALWAYS_BUILT)
def test_amazon_subagent_constructs(attr, expected_name):
    module = __import__(f"app.agents.{attr}", fromlist=[attr])
    agent = getattr(module, attr)
    assert agent is not None, f"{attr} unexpectedly None"
    assert agent.name == expected_name


async def _collect_failures(agent) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for t in agent.tools or []:
        if hasattr(t, "get_tools"):
            for inner in await t.get_tools():
                try:
                    inner._get_declaration()
                except Exception as e:
                    name = getattr(inner, "name", None) or getattr(getattr(inner, "func", None), "__name__", "?")
                    failures.append((name, f"{type(e).__name__}: {e}"))
        elif callable(t):
            try:
                FunctionTool(func=t)._get_declaration()
            except Exception as e:
                failures.append((t.__name__, f"{type(e).__name__}: {e}"))
    return failures


@pytest.mark.parametrize("attr,expected_name", _ALWAYS_BUILT)
@pytest.mark.asyncio
async def test_amazon_subagent_tool_declarations_build(attr, expected_name):
    module = __import__(f"app.agents.{attr}", fromlist=[attr])
    agent = getattr(module, attr)
    failures = await _collect_failures(agent)
    assert not failures, f"{expected_name} tools failed to build:\n" + "\n".join(
        f"  - {n}: {e}" for n, e in failures
    )


def test_bigquery_agent_is_optional():
    """BigQueryAgent is None when BQ_GCP_SERVICE_ACCOUNT_INFO unset; otherwise an LlmAgent."""
    from app.agents.bigquery_agent import bigquery_agent
    if bigquery_agent is not None:
        assert bigquery_agent.name == "BigQueryAgent"


def test_clickup_agent_is_optional():
    """ClickUpAgent is None when CLICKUP_API_TOKEN unset; otherwise an LlmAgent."""
    from app.agents.clickup_agent import clickup_agent
    if clickup_agent is not None:
        assert clickup_agent.name == "ClickUpAgent"
