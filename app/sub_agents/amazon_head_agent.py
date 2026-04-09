"""Amazon Head Agent — domain router for all Amazon business operations.

Delegates to specialized child agents wrapped as AgentTools:
- AmazonAgent: Product research (Keepa, SP-API, Helium10)
- AmazonMemoryAgent: Knowledge storage (Pinecone, Neo4j graph)
- AmazonBigQueryAgent: Business data analytics
- AmazonWorkspaceAgent: Google Drive, Sheets, Calendar
- AmazonDataAnalystAgent: Visualization, charts, data analysis
"""

import logging
import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.adk.tools.agent_tool import AgentTool

from app.app_utils.models import get_model
from app.callbacks.guardrails import prompt_injection_guardrail
from app.toolsets import ScratchpadToolset

from app.sub_agents.amazon_agent import amazon_agent
from app.sub_agents.amazon_memory_agent import amazon_memory_agent
from app.sub_agents.amazon_workspace_agent import amazon_workspace_agent
from app.sub_agents.amazon_data_analyst_agent import amazon_data_analyst_agent
from app.sub_agents.bigquery_agent import bigquery_agent

logger = logging.getLogger(__name__)

_skills_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_routing_skill = load_skill_from_dir(_skills_dir / "amazon-routing-skill")
_scratchpad_skill = load_skill_from_dir(_skills_dir / "scratchpad-skill")

# Wrap child agents as tools — reduces hop count vs transfer_to_agent
_child_tools = [
    skill_toolset.SkillToolset(skills=[_routing_skill, _scratchpad_skill]),
    AgentTool(agent=amazon_agent),
    AgentTool(agent=amazon_memory_agent),
    AgentTool(agent=amazon_workspace_agent),
    AgentTool(agent=amazon_data_analyst_agent),
    ScratchpadToolset(),
]

if bigquery_agent:
    _child_tools.append(AgentTool(agent=bigquery_agent))

amazon_head_agent = Agent(
    name="AmazonHeadAgent",
    model=get_model("AmazonAgent"),
    description=(
        "Amazon business operations manager. Handles ALL Amazon-related tasks: "
        "product research, pricing, Keepa data, SP-API, Helium10 keywords, "
        "BigQuery sales/inventory analytics, professional memory and knowledge graph, "
        "Google Workspace (Drive, Sheets, Calendar), data visualization, charts, and file exports. "
        "Delegate here for anything related to Amazon business. NOT for AI image generation."
    ),
    instruction=(
        "You are the Amazon Head Agent — the central coordinator for all Amazon business operations. "
        "You manage a team of specialist agents, each callable as a tool.\n\n"
        "Load the `amazon-routing-skill` for detailed routing decisions and multi-agent coordination patterns. "
        "Load the `scratchpad-skill` when coordinating data handoffs between agents.\n\n"
        "Your team: AmazonAgent (product research), AmazonMemoryAgent (knowledge/graph), "
        + ("BigQueryAgent (business analytics), " if bigquery_agent else "")
        + "AmazonWorkspaceAgent (Drive/Sheets/Calendar), AmazonDataAnalystAgent (charts/visualization).\n\n"
        "For simple tasks, route directly to the right agent. "
        "For multi-step tasks, use the scratchpad as shared state between agents."
    ),
    tools=_child_tools,
    before_model_callback=prompt_injection_guardrail,
)
