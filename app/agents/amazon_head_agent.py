"""Amazon Head Agent — domain router for all Amazon business operations.

Delegates to specialized child agents via transfer_to_agent:
- AmazonAgent: Product research (Keepa, SP-API, Helium10)
- AmazonMemoryAgent: Knowledge storage (Neo4j graph + memory)
- BigQueryAgent: Business data analytics (conditional on BQ creds)
- AmazonWorkspaceAgent: Google Drive, Sheets, Gmail, Calendar
- AmazonDataAnalystAgent: Visualization, charts, data analysis
"""

from __future__ import annotations

import logging
import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.agents.amazon_agent import amazon_agent
from app.agents.amazon_data_analyst_agent import amazon_data_analyst_agent
from app.agents.amazon_memory_agent import amazon_memory_agent
from app.agents.amazon_workspace_agent import amazon_workspace_agent
from app.agents.bigquery_agent import bigquery_agent
from app.toolsets import ScratchpadToolset
from app.util.models import get_model

logger = logging.getLogger(__name__)

_SKILLS_DIR = pathlib.Path(__file__).parent.parent.parent / "skills"
_routing_skill = load_skill_from_dir(_SKILLS_DIR / "amazon-routing-skill")
_scratchpad_skill = load_skill_from_dir(_SKILLS_DIR / "scratchpad-skill")


amazon_head_agent = Agent(
    name="AmazonHeadAgent",
    model=get_model("AmazonHeadAgent"),
    description=(
        "Amazon business operations manager. Handles ALL Amazon-related tasks: "
        "product research, pricing, Keepa data, SP-API, Helium10 keywords, "
        "BigQuery sales/inventory analytics, knowledge graph, Google Workspace "
        "(Drive, Sheets, Calendar, Gmail), data visualization, charts, and file exports. "
        "Delegate here for anything related to Amazon business. NOT for AI image generation."
    ),
    instruction=(
        "You are the Amazon Head Agent — the central coordinator for all Amazon business operations. "
        "You manage a team of specialist sub-agents. Delegate to the right one via transfer_to_agent.\n\n"
        "Load the `amazon-routing-skill` for detailed routing decisions and multi-agent coordination patterns. "
        "Load the `scratchpad-skill` when coordinating data handoffs between agents.\n\n"
        "Your team: AmazonAgent (product research), AmazonMemoryAgent (knowledge/graph), "
        + ("BigQueryAgent (business analytics), " if bigquery_agent else "")
        + "AmazonWorkspaceAgent (Drive/Sheets/Gmail/Calendar), "
        "AmazonDataAnalystAgent (data analysis, statistics, charts — handles large files other agents can't read).\n\n"
        "For simple tasks, delegate directly to the right agent. "
        "For multi-step tasks, use the scratchpad as shared state between agents."
    ),
    sub_agents=[
        amazon_agent,
        amazon_memory_agent,
        amazon_workspace_agent,
        amazon_data_analyst_agent,
        *([bigquery_agent] if bigquery_agent else []),
    ],
    tools=[
        skill_toolset.SkillToolset(skills=[_routing_skill, _scratchpad_skill]),
        ScratchpadToolset(),
    ],
)
