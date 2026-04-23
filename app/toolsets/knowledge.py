"""Neo4j-backed shared-knowledge toolset.

Kept distinct from:
- `MemoryToolset` (app/toolsets/memory.py) — Ori's local LanceDB memory.
- `GraphToolset` (app/toolsets/graph.py) — graph-traversal primitives.

ACL is enforced per-tool inside `app/tools/memory_tools.py`. Authorship is
stored as a property on each node; update/delete gates use a Cypher
`WHERE node.author_user_id = $caller_id` predicate.

Three node kinds:
- `:Memory` — observations (ideas, procedures, incidents, events).
- `:Person` — actors (the user, colleagues, contacts).
- `:Entity` — referable things (brands, companies, departments, products,
  projects, locations, tools). entity_type is a canonicalized property.
"""

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class KnowledgeToolset(BaseToolset):
    """Shared knowledge base (memories + people + entities) backed by Neo4j."""

    async def get_tools(self, readonly_context=None):
        from app.tools.memory_tools import (
            create_entity,
            create_person,
            create_record,
            delete_any_person,
            delete_entity,
            delete_person,
            delete_record,
            get_records,
            list_records,
            merge_persons,
            promote_person,
            relate_entities,
            relate_persons,
            search_entities,
            search_knowledge,
            search_people,
            update_any_person,
            update_any_record,
            update_entity,
            update_person,
            update_record,
        )

        return [
            # Memory (observations)
            FunctionTool(func=search_knowledge),
            FunctionTool(func=get_records),
            FunctionTool(func=list_records),
            FunctionTool(func=create_record),
            FunctionTool(func=update_record),
            FunctionTool(func=update_any_record),
            FunctionTool(func=delete_record),
            # Person (actors)
            FunctionTool(func=create_person),
            FunctionTool(func=search_people),
            FunctionTool(func=update_person),
            FunctionTool(func=update_any_person),
            FunctionTool(func=delete_person),
            FunctionTool(func=delete_any_person),
            FunctionTool(func=promote_person),
            FunctionTool(func=merge_persons),
            FunctionTool(func=relate_persons),
            # Entity (referable things)
            FunctionTool(func=create_entity),
            FunctionTool(func=search_entities),
            FunctionTool(func=update_entity),
            FunctionTool(func=delete_entity),
            FunctionTool(func=relate_entities),
        ]
