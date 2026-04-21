from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class GoogleWorkspaceToolset(BaseToolset):
    """Google Drive, Sheets, Calendar, and Gmail tools with per-user OAuth2."""

    async def get_tools(self, readonly_context=None):
        from app.tools.google_drive import (
            google_connect,
            google_connect_complete,
            google_disconnect,
            drive_list_files,
            drive_download_file,
            sheets_read,
            sheets_write,
            sheets_create,
        )
        from app.tools.google_calendar import (
            calendar_list,
            calendar_list_events,
            calendar_create_event,
            calendar_update_event,
            calendar_delete_event,
        )
        from app.tools.google_gmail import (
            gmail_list_messages,
            gmail_get_message,
            gmail_list_threads,
            gmail_get_thread,
            gmail_list_labels,
            gmail_download_attachment,
        )

        return [
            FunctionTool(func=google_connect),
            FunctionTool(func=google_connect_complete),
            FunctionTool(func=google_disconnect),
            FunctionTool(func=drive_list_files),
            FunctionTool(func=drive_download_file),
            FunctionTool(func=sheets_read),
            FunctionTool(func=sheets_write),
            FunctionTool(func=sheets_create),
            FunctionTool(func=calendar_list),
            FunctionTool(func=calendar_list_events),
            FunctionTool(func=calendar_create_event),
            FunctionTool(func=calendar_update_event),
            FunctionTool(func=calendar_delete_event),
            FunctionTool(func=gmail_list_messages),
            FunctionTool(func=gmail_get_message),
            FunctionTool(func=gmail_list_threads),
            FunctionTool(func=gmail_get_thread),
            FunctionTool(func=gmail_list_labels),
            FunctionTool(func=gmail_download_attachment),
        ]
