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
        "- **AmazonAgent**: Product research. Keepa data (pricing, sales, competitors, BSR), "
        "SP-API (catalog, listings, competitive pricing, reports), Helium10 (Cerebro/Magnet keyword analysis).\n"
        "- **AmazonMemoryAgent**: Professional knowledge. Pinecone (semantic search, records, people), "
        "Neo4j graph (entities, relationships, connection paths, timelines), auto-extraction control.\n"
        + (
            "- **BigQueryAgent**: Business analytics. SQL on sales, inventory, advertising, operational data. "
            "Has per-table access control — non-admin users are gated by email domain.\n"
            if bigquery_agent else ""
        ) +
        "- **AmazonWorkspaceAgent**: Google Workspace. Drive files, Sheets read/write, Calendar events.\n"
        "- **AmazonDataAnalystAgent**: Visualization and analysis. Charts, plots, CSV/Excel exports, "
        "image generation, statistical analysis.\n\n"

        "ROUTING GUIDE:\n"
        "- 'What is the price of ASIN X?' → AmazonAgent\n"
        "- 'What do we know about supplier X?' → AmazonMemoryAgent (search first), "
        "then AmazonAgent (fetch fresh if not found)\n"
        "- 'How is X connected to Y?' → AmazonMemoryAgent (graph query)\n"
        "- 'Remember this supplier/person/product' → AmazonMemoryAgent (create_record/create_person/add_entity)\n"
        "- 'What were last month's sales?' → BigQueryAgent\n"
        "- 'Chart the sales trend' → BigQueryAgent (query) → scratchpad → AmazonDataAnalystAgent (chart)\n"
        "- 'Export this to Sheets' → AmazonWorkspaceAgent\n"
        "- 'Create a chart/plot/image' → AmazonDataAnalystAgent\n"
        "- 'What's on the calendar?' → AmazonWorkspaceAgent\n\n"

        "MULTI-AGENT COORDINATION:\n"
        "When a task spans multiple agents, use the scratchpad as shared state:\n"
        "1. Have the data-producing agent write results to scratchpad (e.g. `scratchpad_write`).\n"
        "2. Have the consuming agent read from scratchpad (e.g. `scratchpad_read`).\n"
        "Example: AmazonAgent fetches Keepa data → writes to scratchpad → "
        "AmazonDataAnalystAgent reads and charts it.\n\n"

        "MEMORY AUTHORSHIP: When storing records on behalf of the user, set author to their user ID. "
        "When YOU decide to store something, set author to 'agent'.\n\n"

        "RULES:\n"
        "- Never fabricate data. If an agent returns an error, report it immediately.\n"
        "- For simple single-agent tasks, call the appropriate agent directly — don't overthink routing.\n"
        "- Always include the relevant details from agent responses in your final answer.\n"
        "- If unsure which agent to use, prefer AmazonMemoryAgent for knowledge queries "
        "and AmazonAgent for fresh product data.\n"
    ),
    tools=_child_tools,
    before_model_callback=prompt_injection_guardrail,
)
