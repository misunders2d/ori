from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class SchedulingToolset(BaseToolset):
    """Core scheduling and timing tools."""

    async def get_tools(self, readonly_context=None):
        from app.tools.scheduling import (
            delete_scheduled_task,
            edit_scheduled_task,
            get_current_time,
            get_scheduled_task_logs,
            list_scheduled_tasks,
            schedule_one_off_task,
            schedule_recurring_task,
        )

        return [
            FunctionTool(func=get_current_time),
            FunctionTool(func=schedule_one_off_task),
            FunctionTool(func=schedule_recurring_task),
            FunctionTool(func=list_scheduled_tasks),
            FunctionTool(func=edit_scheduled_task),
            FunctionTool(func=delete_scheduled_task),
            FunctionTool(func=get_scheduled_task_logs),
        ]
