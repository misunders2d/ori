"""Smoke tests for every toolset.

Each toolset's `get_tools()` is async and lazy-imports its tool functions —
which means a stale import (e.g. a tool that was renamed or removed) only
fails at agent-construction time, not at module load. These tests exercise
that path so import bugs surface in CI, not on first live boot.
"""

import pytest

from app.toolsets.clickup import ClickUpToolset
from app.toolsets.evolution import EvolutionToolset
from app.toolsets.github import GitHubToolset
from app.toolsets.google_workspace import GoogleWorkspaceToolset
from app.toolsets.graph import GraphToolset
from app.toolsets.h10 import H10Toolset
from app.toolsets.integration import IntegrationToolset
from app.toolsets.keepa import KeepaToolset
from app.toolsets.knowledge import KnowledgeToolset
from app.toolsets.memory import MemoryToolset
from app.toolsets.planner import PlannerToolset
from app.toolsets.scheduling import SchedulingToolset
from app.toolsets.scratchpad import ScratchpadToolset
from app.toolsets.sp_api import SPApiToolset
from app.toolsets.system import SystemToolset
from app.toolsets.visualization import CreativesToolset, VisualizationToolset


@pytest.mark.parametrize(
    "toolset_cls",
    [
        ClickUpToolset,
        CreativesToolset,
        EvolutionToolset,
        GitHubToolset,
        GoogleWorkspaceToolset,
        GraphToolset,
        H10Toolset,
        IntegrationToolset,
        KeepaToolset,
        KnowledgeToolset,
        MemoryToolset,
        PlannerToolset,
        SPApiToolset,
        SchedulingToolset,
        ScratchpadToolset,
        SystemToolset,
        VisualizationToolset,
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
