"""Amazon Memory sub-agent — Neo4j knowledge base with native vector search.

Stores and retrieves memories + people in Neo4j alone. Access control is
namespace-scoped (personal / professional / technical for memories, personal /
professional for people) and enforced in code. Authorship is stored as the
`author_user_id` property on each node (plus `via_bot` and `created_at`); the
caller's `:Person` node is auto-provisioned on first tool call and remains the
canonical identity, and update/delete run a property-predicate Cypher MATCH.
"""

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.app_utils.models import get_model
from app.callbacks.guardrails import prompt_injection_guardrail, tool_output_spillover_guardrail
from app.toolsets import KnowledgeToolset, ScratchpadToolset

_base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_knowledge_graph_skill = load_skill_from_dir(_base_dir / "knowledge-graph-skill")

amazon_memory_agent = Agent(
    name="AmazonMemoryAgent",
    model=get_model("AmazonMemoryAgent"),
    description=(
        "Knowledge specialist. Stores and retrieves memories, people, and their "
        "relationships in Neo4j. Three memory namespaces (personal / professional / "
        "technical) and two people scopes (personal / professional); admins see all, "
        "company-domain users see professional + technical, everyone else sees "
        "technical only. Only the creator (or admins) can modify a record."
    ),
    instruction=(
        "You are the knowledge specialist. "
        "Load the `knowledge-graph-skill` for architecture, tool reference, "
        "namespace rules, and workflow examples.\n\n"
        "Use the memory tools for all storage and retrieval: `create_record`, "
        "`create_person`, `search_knowledge`, `search_people`, `get_records`, "
        "`list_records`, `update_record`, `update_person`, `delete_record`. "
        "The `update_any_record`, `update_any_person`, and `promote_person` tools "
        "are admin-only overrides — only call them when explicitly needed.\n\n"
        "If a tool returns `{status: \"forbidden\"}`, relay the message to the user "
        "unchanged — do NOT retry with a different tool. If a tool returns "
        "`{status: \"error\"}`, report the exact error text verbatim.\n\n"
        "ROUTING FALLBACK: If the user's request is outside knowledge graph / "
        "memory / people (e.g. product research, charts, BigQuery SQL, "
        "Drive/Sheets, decks, ClickUp, code), call "
        "`transfer_to_agent(agent_name='AmazonHeadAgent')` so the head can "
        "re-route. Do not refuse, guess, or answer outside your domain."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[_knowledge_graph_skill]),
        KnowledgeToolset(),
        ScratchpadToolset(),
    ],
    before_model_callback=prompt_injection_guardrail,
    after_tool_callback=tool_output_spillover_guardrail,
)
