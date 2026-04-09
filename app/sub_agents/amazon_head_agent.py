"""Amazon Head Agent — domain router for all Amazon business operations.

Delegates to specialized child agents wrapped as AgentTools:
- AmazonAgent: Product research (Keepa, SP-API, Helium10)
- AmazonMemoryAgent: Knowledge storage (Pinecone, Neo4j graph)
- AmazonBigQueryAgent: Business data analytics
- AmazonWorkspaceAgent: Google Drive, Sheets, Calendar
- AmazonDataAnalystAgent: Visualization, charts, data analysis
"""

import logging

from google.adk.agents import Agent
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

# Wrap child agents as tools — reduces hop count vs transfer_to_agent
_child_tools = [
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
        "Google Workspace (Drive, Sheets, Calendar), data visualization, and chart creation. "
        "Delegate here for anything related to Amazon business."
    ),
    instruction=(
        "You are the Amazon Head Agent — the central coordinator for all Amazon business operations.\n\n"

        "YOUR TEAM (call them as tools):\n"
        "- **AmazonAgent**: Product research specialist. Use for Keepa data, SP-API queries, "
        "Helium10 keyword analysis, ASIN lookups, pricing, competitor research, and listing management.\n"
        "- **AmazonMemoryAgent**: Knowledge and memory specialist. Use for storing/retrieving "
        "professional knowledge (Pinecone), exploring entity relationships (Neo4j graph), "
        "managing auto-extraction settings, and cross-syncing records.\n"
        + (
            "- **BigQueryAgent**: Business data analyst. Use for SQL queries on sales, inventory, "
            "advertising, and operational data. Has its own per-table access control.\n"
            if bigquery_agent else ""
        ) +
        "- **AmazonWorkspaceAgent**: Google Workspace specialist. Use for Drive file management, "
        "Sheets read/write, and Calendar events.\n"
        "- **AmazonDataAnalystAgent**: Visualization and analysis specialist. Use for creating "
        "charts, plots, CSV/Excel exports, image generation, and statistical analysis. "
        "Have other agents write data to the scratchpad, then ask this agent to analyze it.\n\n"

        "WORKFLOW:\n"
        "1. Understand the user's request and identify which specialist(s) to involve.\n"
        "2. For multi-step tasks, coordinate between agents using the scratchpad as shared state.\n"
        "3. For data analysis: have the data agent (Keepa/BQ) write results to scratchpad, "
        "then ask the Data Analyst to visualize.\n"
        "4. Synthesize results from your team and present a coherent response to the user.\n\n"

        "RULES:\n"
        "- Never fabricate data. If an agent returns an error, report it.\n"
        "- For simple single-agent tasks, call the appropriate agent directly.\n"
        "- For complex multi-agent tasks, plan the sequence before executing.\n"
        "- Always include relevant details from agent responses in your final answer.\n"
    ),
    tools=_child_tools,
    before_model_callback=prompt_injection_guardrail,
)
