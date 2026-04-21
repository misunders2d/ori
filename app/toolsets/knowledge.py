"""Neo4j-backed shared-knowledge toolset — replaces PineconeToolset.

Kept distinct from:
- `MemoryToolset` (app/toolsets/memory.py) — Ori's local LanceDB memory.
- `GraphToolset` (app/toolsets/graph.py) — graph-traversal primitives.

ACL is enforced per-tool inside `app/tools/memory_tools.py`. This toolset
is not yet wired into any sub-agent — Sprint B swaps the PineconeToolset
import in `app/sub_agents/amazon_memory_agent.py`.
"""

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class KnowledgeToolset(BaseToolset):
    """Shared knowledge base (memories + people) backed by Neo4j with native vector search."""

    async def get_tools(self, readonly_context=None):
        from app.tools.memory_tools import (
            create_person,
            create_record,
            delete_any_person,
            delete_person,
            delete_record,
            get_records,
            list_records,
            merge_persons,
            promote_person,
            search_knowledge,
            search_people,
            update_any_person,
            update_any_record,
            update_person,
            update_record,
        )

        return [
            FunctionTool(func=search_knowledge),
            FunctionTool(func=get_records),
            FunctionTool(func=list_records),
            FunctionTool(func=create_record),
            FunctionTool(func=update_record),
            FunctionTool(func=update_any_record),
            FunctionTool(func=delete_record),
            FunctionTool(func=create_person),
            FunctionTool(func=search_people),
            FunctionTool(func=update_person),
            FunctionTool(func=update_any_person),
            FunctionTool(func=delete_person),
            FunctionTool(func=delete_any_person),
            FunctionTool(func=promote_person),
            FunctionTool(func=merge_persons),
        ]
