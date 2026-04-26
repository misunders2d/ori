from google.adk.tools import load_artifacts
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class GoogleWorkspaceToolset(BaseToolset):
    """Google Drive, Sheets, Calendar, and Gmail tools with per-user OAuth2."""

    async def get_tools(self, readonly_context=None):
        from app.tools.google_calendar import (
            calendar_create_event,
            calendar_delete_event,
            calendar_list,
            calendar_list_events,
            calendar_update_event,
        )
        from app.tools.google_drive import (
            drive_download_file,
            drive_list_files,
            google_connect,
            google_disconnect,
            sheets_create,
            sheets_read,
            sheets_write,
        )
        from app.tools.google_gmail import (
            gmail_download_attachment,
            gmail_get_message,
            gmail_get_thread,
            gmail_list_labels,
            gmail_list_messages,
            gmail_list_threads,
        )

        return [
            FunctionTool(func=google_connect),
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
            load_artifacts,
        ]
