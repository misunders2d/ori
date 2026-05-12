"""Amazon Head Agent — domain router for all Amazon business operations.

Delegates to specialized child agents via transfer_to_agent:
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

from app.app_utils.models import get_model
from app.callbacks.guardrails import (
    file_attachment_capture,
    prompt_injection_guardrail,
    tool_output_spillover_guardrail,
)
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

amazon_head_agent = Agent(
    name="AmazonHeadAgent",
    model=get_model("AmazonHeadAgent"),
    description=(
        "Amazon business operations manager. Handles ALL Amazon-related tasks: "
        "product research, pricing, Keepa data, SP-API, Helium10 keywords, "
        "BigQuery sales/inventory analytics, professional memory and knowledge graph, "
        "Google Workspace (Drive, Sheets, Calendar), data visualization, charts, CSV/Excel exports, "
        "and PowerPoint presentation (.pptx) building via AmazonDataAnalystAgent. "
        "Delegate here for anything related to Amazon business. NOT for AI image generation."
    ),
    instruction=(
        "You are the Amazon Head Agent — the central coordinator for all Amazon business operations. "
        "You manage a team of specialist sub-agents. Delegate to the right one via transfer_to_agent.\n\n"
        "Load the `amazon-routing-skill` for detailed routing decisions and multi-agent coordination patterns. "
        "Load the `scratchpad-skill` when coordinating data handoffs between agents.\n\n"
        "Your team: AmazonAgent (product research), AmazonMemoryAgent (knowledge/graph), "
        + ("BigQueryAgent (business analytics), " if bigquery_agent else "")
        + "AmazonWorkspaceAgent (Drive/Sheets/Calendar), "
        "AmazonDataAnalystAgent (data analysis, statistics, charts, CSV/Excel exports, AND "
        "PowerPoint presentation (.pptx) building via `generate_presentation` — also handles "
        "large files other agents can't read).\n\n"
        "ROUTING HINTS:\n"
        "- Any 'deck / slides / presentation / PowerPoint / PPTX' request → AmazonDataAnalystAgent.\n"
        "- Sales/orders/ads totals → AmazonAgent (uses SP-API report path, NEVER list_orders).\n"
        "- 'Today' / 'yesterday' / any naked day reference means Pacific (America/Los_Angeles) "
        "unless the user specifies otherwise.\n\n"
        "For simple tasks, delegate directly to the right agent. "
        "For multi-step tasks, use the scratchpad as shared state between agents.\n\n"
        "ROUTING FALLBACK: If the user's request is outside Amazon business "
        "operations (e.g. ClickUp tasks, A2A communication, self-evolution / "
        "code changes, AI image generation, scheduling / contracts), call "
        "`transfer_to_agent(agent_name='CoordinatorAgent')` so the coordinator "
        "can re-route. Do not refuse, guess, or answer outside your domain. System / lifecycle requests (reboot, restart, rollback, update, shut down) are ALWAYS outside your domain — bounce immediately, do not invent a refusal."
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
    before_model_callback=prompt_injection_guardrail,
    after_tool_callback=[tool_output_spillover_guardrail, file_attachment_capture],
)
