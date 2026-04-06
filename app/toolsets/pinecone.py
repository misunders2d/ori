from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class PineconeToolset(BaseToolset):
    """Pinecone vector knowledge base — search, create, update, delete records."""

    async def get_tools(self, readonly_context=None):
        from app.tools.pinecone_tools import (
            search_knowledge,
            get_records,
            list_records,
            create_record,
            create_person,
            update_record,
            delete_record,
        )

        return [
            FunctionTool(func=search_knowledge),
            FunctionTool(func=get_records),
            FunctionTool(func=list_records),
            FunctionTool(func=create_record),
            FunctionTool(func=create_person),
            FunctionTool(func=update_record),
            FunctionTool(func=delete_record),
        ]
