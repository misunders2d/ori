"""Amazon Memory sub-agent — Pinecone vector search and Neo4j knowledge graph.

Handles professional memory storage, entity tracking, relationship mapping,
and graph-based knowledge queries for the Amazon domain.
"""

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.app_utils.models import get_model
from app.callbacks.guardrails import prompt_injection_guardrail
from app.toolsets import PineconeToolset, ScratchpadToolset
from app.toolsets.graph import GraphToolset

_base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_knowledge_graph_skill = load_skill_from_dir(_base_dir / "knowledge-graph-skill")

amazon_memory_agent = Agent(
    name="AmazonMemoryAgent",
    model=get_model("AmazonAgent"),
    description=(
        "Professional memory and knowledge graph specialist. Manages Pinecone vector "
        "records (semantic search, people, products, concepts) and Neo4j graph "
        "(entities, relationships, connection paths, timelines). Use for storing, "
        "retrieving, or exploring professional knowledge and entity relationships."
    ),
    instruction=(
        "You are the Memory & Knowledge Graph agent for the Amazon domain.\n\n"

        "YOUR TOOLS:\n"
        "- **Pinecone**: Semantic search and CRUD for knowledge records. Use `search_knowledge` "
        "to find relevant records, `create_record` / `create_person` to store new information, "
        "`update_record` / `delete_record` to maintain accuracy.\n"
        "- **Neo4j Graph**: Entity and relationship tracking. Use `add_entity` to create/upsert "
        "entities (person, company, project, product, concept, event). Use `link_entities` to "
        "create directed relationships. Use `query_connections`, `find_connection_path`, "
        "`entity_timeline`, `search_graph` for exploration.\n"
        "- **Auto-extraction**: Use `enable_auto_extraction` / `disable_auto_extraction` to "
        "control background entity extraction per session. Use `list_auto_extraction_sessions` "
        "to see which sessions have it enabled.\n"
        "- **Cross-sync**: Use `import_pinecone_record` to sync a Pinecone record into the graph.\n\n"

        "MEMORY AUTHORSHIP: When the user explicitly asks to store something, set author to "
        "their user ID. When storing something on your own initiative, set author to 'agent'.\n\n"

        "GUIDELINES:\n"
        "- For semantic content search (what was said about X), use Pinecone.\n"
        "- For relationship queries (how is X connected to Y), use the graph.\n"
        "- When creating Pinecone records with relationships, they auto-sync to the graph.\n"
        "- Report errors immediately — never fabricate data.\n"
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[_knowledge_graph_skill]),
        PineconeToolset(),
        GraphToolset(),
        ScratchpadToolset(),
    ],
    before_model_callback=prompt_injection_guardrail,
)
