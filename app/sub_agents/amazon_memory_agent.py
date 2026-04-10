"""Amazon Memory sub-agent — Pinecone vector search with automatic Neo4j mirroring.

Handles professional memory storage and retrieval for the Amazon domain. The
knowledge graph is kept in sync automatically behind the Pinecone tools; the
agent only interacts with Pinecone directly. Auto-extraction of entities from
conversations can be toggled per session.
"""

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.adk.tools.function_tool import FunctionTool

from app.app_utils.models import get_model
from app.callbacks.guardrails import prompt_injection_guardrail
from app.tools.graph_tools import (
    disable_auto_extraction,
    enable_auto_extraction,
    list_auto_extraction_sessions,
)
from app.toolsets import PineconeToolset, ScratchpadToolset

_base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_knowledge_graph_skill = load_skill_from_dir(_base_dir / "knowledge-graph-skill")

amazon_memory_agent = Agent(
    name="AmazonMemoryAgent",
    model=get_model("AmazonMemoryAgent"),
    description=(
        "Professional memory specialist. Stores and retrieves knowledge, people, "
        "products, and concepts via Pinecone semantic search. A knowledge graph is "
        "mirrored automatically behind the scenes — the agent does not manipulate "
        "it directly. Can also toggle background auto-extraction of entities from "
        "chat sessions on request."
    ),
    instruction=(
        "You are the professional memory specialist. "
        "Load the `knowledge-graph-skill` for the memory architecture, tool reference, "
        "authorship rules, and workflow examples.\n\n"
        "Use Pinecone tools (`create_record`, `create_person`, `update_record`, "
        "`delete_record`, `search_knowledge`, `get_records`, `list_records`) for all "
        "memory operations. Graph mirroring is automatic — you never edit Neo4j directly.\n\n"
        "Auto-extraction is OFF by default for every chat. Only enable or disable it "
        "when the user explicitly asks, using `enable_auto_extraction` / "
        "`disable_auto_extraction` / `list_auto_extraction_sessions`.\n\n"
        "If any tool returns an error, report it immediately — never fabricate data."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[_knowledge_graph_skill]),
        PineconeToolset(),
        FunctionTool(func=enable_auto_extraction),
        FunctionTool(func=disable_auto_extraction),
        FunctionTool(func=list_auto_extraction_sessions),
        ScratchpadToolset(),
    ],
    before_model_callback=prompt_injection_guardrail,
)
