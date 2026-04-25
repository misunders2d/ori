"""Guard: every tool exposed to every agent must build its function-call
declaration without errors.

ADK 2.0's automatic function calling rejects unsupported parameter types
(Union, Any, complex generics, custom classes) at declaration-build time —
during _process_agent_tools, BEFORE the LLM is invoked. A single broken tool
declaration kills the entire agent turn.

We hit this live with `call_friend(message: Union[str, types.Content])` —
the bot couldn't load its tools, every A2A interaction failed, and the
errors looked unrelated. This test would have caught it at CI time.
"""

import pytest
from google.adk.tools.function_tool import FunctionTool


async def _collect_failures(agent) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for t in agent.tools or []:
        if hasattr(t, "get_tools"):
            for inner in await t.get_tools():
                try:
                    inner._get_declaration()
                except Exception as e:  # noqa: BLE001
                    name = getattr(inner, "name", None) or getattr(getattr(inner, "func", None), "__name__", "?")
                    failures.append((name, f"{type(e).__name__}: {e}"))
        elif callable(t):
            try:
                FunctionTool(func=t)._get_declaration()
            except Exception as e:  # noqa: BLE001
                failures.append((t.__name__, f"{type(e).__name__}: {e}"))
    return failures


@pytest.mark.asyncio
async def test_coordinator_tool_declarations_build():
    from app.agents.coordinator import root_agent
    failures = await _collect_failures(root_agent)
    assert not failures, "CoordinatorAgent tools failed to build:\n" + "\n".join(
        f"  - {n}: {e}" for n, e in failures
    )


@pytest.mark.asyncio
async def test_developer_tool_declarations_build():
    from app.agents.developer import developer_agent
    failures = await _collect_failures(developer_agent)
    assert not failures, "DeveloperAgent tools failed to build:\n" + "\n".join(
        f"  - {n}: {e}" for n, e in failures
    )


@pytest.mark.asyncio
async def test_knowledge_tool_declarations_build():
    from app.agents.knowledge import knowledge_agent
    failures = await _collect_failures(knowledge_agent)
    assert not failures, "KnowledgeAgent tools failed to build:\n" + "\n".join(
        f"  - {n}: {e}" for n, e in failures
    )
