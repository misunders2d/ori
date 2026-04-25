"""Smoke tests for every toolset.

Each toolset's `get_tools()` is async and lazy-imports its tool functions —
which means a stale import (e.g. a tool that was renamed or removed) only
fails at agent-construction time, not at module load. These tests exercise
that path so import bugs surface in CI, not on first live boot.
"""

import pytest

from app.toolsets.evolution import EvolutionToolset
from app.toolsets.github import GitHubToolset
from app.toolsets.integration import IntegrationToolset
from app.toolsets.memory import MemoryToolset
from app.toolsets.planner import PlannerToolset
from app.toolsets.scheduling import SchedulingToolset
from app.toolsets.system import SystemToolset


@pytest.mark.parametrize(
    "toolset_cls",
    [
        EvolutionToolset,
        GitHubToolset,
        IntegrationToolset,
        MemoryToolset,
        PlannerToolset,
        SchedulingToolset,
        SystemToolset,
    ],
)
@pytest.mark.asyncio
async def test_toolset_get_tools_resolves(toolset_cls):
    """Each toolset must construct + import its tools without error."""
    ts = toolset_cls()
    tools = await ts.get_tools()
    assert len(tools) > 0, f"{toolset_cls.__name__} returned zero tools"
    for tool in tools:
        # Every entry should be a callable tool with a name we can resolve
        assert hasattr(tool, "name") or hasattr(tool, "func"), (
            f"{toolset_cls.__name__} returned non-tool object: {tool!r}"
        )
