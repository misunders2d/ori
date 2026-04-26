from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class ClickUpToolset(BaseToolset):
    """ClickUp task management — workspace discovery, task CRUD, comments."""

    async def get_tools(self, readonly_context=None):
        from app.tools.clickup import (
            clickup_add_comment,
            clickup_create_task,
            clickup_delete_task,
            clickup_get_task,
            clickup_get_workspace,
            clickup_list_folders_and_lists,
            clickup_list_tasks,
            clickup_task_link,
            clickup_timestamp,
            clickup_update_task,
        )

        return [
            FunctionTool(func=clickup_get_workspace),
            FunctionTool(func=clickup_list_folders_and_lists),
            FunctionTool(func=clickup_list_tasks),
            FunctionTool(func=clickup_get_task),
            FunctionTool(func=clickup_create_task),
            FunctionTool(func=clickup_update_task),
            FunctionTool(func=clickup_add_comment),
            FunctionTool(func=clickup_delete_task),
            FunctionTool(func=clickup_timestamp),
            FunctionTool(func=clickup_task_link),
        ]
