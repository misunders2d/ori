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
        "You are the professional memory and knowledge graph specialist. "
        "Load the `knowledge-graph-skill` for the full memory architecture, tool reference, "
        "authorship rules, entity types, relationship types, and workflow examples.\n\n"
        "Pinecone = semantic content search. Neo4j = entity relationships. "
        "If any tool returns an error, report it immediately — never fabricate data."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[_knowledge_graph_skill]),
        PineconeToolset(),
        GraphToolset(),
        ScratchpadToolset(),
    ],
    before_model_callback=prompt_injection_guardrail,
)
