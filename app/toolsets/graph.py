from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class GraphToolset(BaseToolset):
    """Neo4j knowledge graph — entity tracking, relationships, and graph queries."""

    async def get_tools(self, readonly_context=None):
        from app.tools.graph_tools import (
            add_entity,
            link_entities,
            query_connections,
            find_connection_path,
            entity_timeline,
            search_graph,
            import_pinecone_record,
        )

        return [
            FunctionTool(func=add_entity),
            FunctionTool(func=link_entities),
            FunctionTool(func=query_connections),
            FunctionTool(func=find_connection_path),
            FunctionTool(func=entity_timeline),
            FunctionTool(func=search_graph),
            FunctionTool(func=import_pinecone_record),
        ]
