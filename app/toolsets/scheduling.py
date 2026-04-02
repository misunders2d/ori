from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class SchedulingToolset(BaseToolset):
    """Groups all scheduling, timing, and background task tools."""

    async def get_tools(self, readonly_context=None):
        from app.tools.scheduling import (
            get_current_time,
            schedule_one_off_task,
            schedule_recurring_task,
            list_scheduled_tasks,
            edit_scheduled_task,
            delete_scheduled_task,
            schedule_system_task,
            schedule_recurring_system_task,
            run_system_task_now,
        )
        from app.tools.diagnostics import check_active_tasks

        return [
            FunctionTool(func=get_current_time),
            FunctionTool(func=schedule_one_off_task),
            FunctionTool(func=schedule_recurring_task),
            FunctionTool(func=list_scheduled_tasks),
            FunctionTool(func=edit_scheduled_task),
            FunctionTool(func=delete_scheduled_task),
            FunctionTool(func=schedule_system_task),
            FunctionTool(func=schedule_recurring_system_task),
            FunctionTool(func=run_system_task_now),
            FunctionTool(func=check_active_tasks),
        ]
