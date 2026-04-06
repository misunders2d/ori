from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class GoogleWorkspaceToolset(BaseToolset):
    """Google Drive and Sheets tools with per-user OAuth2."""

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

        return [
            FunctionTool(func=google_connect),
            FunctionTool(func=google_connect_complete),
            FunctionTool(func=google_disconnect),
            FunctionTool(func=drive_list_files),
            FunctionTool(func=drive_download_file),
            FunctionTool(func=sheets_read),
            FunctionTool(func=sheets_write),
            FunctionTool(func=sheets_create),
        ]
