"""Google Drive and Sheets tools — per-user OAuth2 access.

Each tool resolves the current user's email from session state,
retrieves their OAuth2 token, and makes authenticated API calls.
"""

import logging
from typing import Optional

import httpx
from google.adk.tools.tool_context import ToolContext

from app.tools.google_oauth.web_flow import refresh_access_token, start_auth_flow
from app.tools.google_oauth.token_store import (
    get_token, save_token, delete_token,
    save_user_mapping, resolve_email, delete_user_mapping,
)

logger = logging.getLogger(__name__)

_DRIVE_API = "https://www.googleapis.com/drive/v3"
_SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"


def _get_user_email(tool_context: ToolContext) -> str:
    """Resolve the current user's Google email.

    For Slack users, user_id is already an email. For Telegram users,
    user_id is a platform ID (e.g. tg_330959414) which we resolve to
    their Google email via the mapping table.
    """
    if not tool_context:
        return ""
    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    user_id = state.get("user_id", "")
    if not user_id:
        return ""
    # resolve_email returns the ID as-is if it's already an email,
    # otherwise looks up the platform_id → email mapping
    return resolve_email(user_id)


async def _get_valid_token(email: str) -> Optional[str]:
    """Get a valid access token for the user, refreshing if expired."""
    stored = get_token(email)
    if not stored:
        return None

    if not stored["expired"]:
        return stored["access_token"]

    # Token expired — refresh it
    result = await refresh_access_token(stored["refresh_token"])
    if result["status"] == "success":
        save_token(email, result["access_token"], stored["refresh_token"], result["expires_in"], stored["scopes"])
        return result["access_token"]

    logger.error("Token refresh failed for %s: %s", email, result.get("message"))
    return None


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

async def google_connect(tool_context: ToolContext = None) -> dict:
    """Start Google authorization for the current user (Drive/Sheets/Calendar/Gmail).

    Returns an authorization URL. The user opens it in a browser and grants
    access — Google then redirects to /oauth/google/callback which persists
    the tokens automatically. No follow-up tool call is needed.
    """
    state = tool_context.state.to_dict() if (tool_context and hasattr(tool_context.state, "to_dict")) else {}
    user_id = state.get("user_id", "")
    if not user_id:
        return {"status": "error", "message": "No user_id in session state — cannot start OAuth flow."}

    result = start_auth_flow(user_id)
    if result["status"] != "success":
        return result

    return {
        "status": "success",
        "message": (
            "Open this link in your browser to connect your Google account "
            "(Drive, Sheets, Calendar, Gmail read-only):\n\n"
            f"{result['auth_url']}\n\n"
            "After you authorize, you'll see a success page — then come back here."
        ),
    }


async def google_disconnect(tool_context: ToolContext = None) -> dict:
    """Disconnect Google Drive/Sheets for the current user. Removes stored tokens and mapping."""
    email = _get_user_email(tool_context)
    if not email:
        return {"status": "error", "message": "Could not determine user. No Google account linked."}

    # Clean up both the token and the platform ID mapping
    delete_token(email)
    if tool_context:
        state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
        user_id = state.get("user_id", "")
        if user_id and user_id != email:
            delete_user_mapping(user_id)

    return {"status": "success", "message": f"Google account disconnected for {email}."}


# ---------------------------------------------------------------------------
# Drive tools
# ---------------------------------------------------------------------------

async def drive_list_files(
    query: str = "",
    folder_id: str = "",
    max_results: int = 20,
    tool_context: ToolContext = None,
) -> dict:
    """List or search files in Google Drive.

    Args:
        query: Search query (e.g., 'name contains "report"', 'mimeType = "application/vnd.google-apps.spreadsheet"').
               Leave empty to list recent files.
        folder_id: Optional folder ID to search within.
        max_results: Maximum number of results (default 20).
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    q_parts = []
    if query:
        q_parts.append(query)
    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")
    q_parts.append("trashed = false")

    params = {
        "q": " and ".join(q_parts),
        "pageSize": min(max_results, 100),
        "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
        "orderBy": "modifiedTime desc",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{_DRIVE_API}/files", params=params, headers=_auth_headers(token))
            resp.raise_for_status()
            data = resp.json()
            files = data.get("files", [])
            return {"status": "success", "count": len(files), "files": files}
    except Exception as e:
        return {"status": "error", "message": f"Drive API error: {e}"}


async def drive_download_file(
    file_id: str,
    tool_context: ToolContext = None,
) -> dict:
    """Download the content of a file from Google Drive.

    For Google Docs/Sheets/Slides, exports as PDF. For regular files, downloads directly.

    Args:
        file_id: The Google Drive file ID.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Get file metadata first
            meta_resp = await client.get(
                f"{_DRIVE_API}/files/{file_id}",
                params={"fields": "name,mimeType"},
                headers=_auth_headers(token),
            )
            meta_resp.raise_for_status()
            meta = meta_resp.json()
            mime = meta.get("mimeType", "")
            name = meta.get("name", "file")

            # Google Workspace files need export
            if mime.startswith("application/vnd.google-apps."):
                export_mime = "application/pdf"
                if "spreadsheet" in mime:
                    export_mime = "text/csv"
                resp = await client.get(
                    f"{_DRIVE_API}/files/{file_id}/export",
                    params={"mimeType": export_mime},
                    headers=_auth_headers(token),
                )
            else:
                resp = await client.get(
                    f"{_DRIVE_API}/files/{file_id}",
                    params={"alt": "media"},
                    headers=_auth_headers(token),
                )
            resp.raise_for_status()

            # Save to tmp
            import os
            os.makedirs("./tmp/drive_downloads", exist_ok=True)
            ext = ".csv" if "csv" in (export_mime if mime.startswith("application/vnd.google-apps.") else mime) else ""
            path = f"./tmp/drive_downloads/{name}{ext}"
            with open(path, "wb") as f:
                f.write(resp.content)

            return {"status": "success", "file_path": path, "filename": name, "size_bytes": len(resp.content)}
    except Exception as e:
        return {"status": "error", "message": f"Drive download error: {e}"}


# ---------------------------------------------------------------------------
# Sheets tools
# ---------------------------------------------------------------------------

async def sheets_read(
    spreadsheet_id: str,
    range: str = "Sheet1",
    tool_context: ToolContext = None,
) -> dict:
    """Read data from a Google Spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet ID (from the URL).
        range: Cell range in A1 notation (e.g., 'Sheet1!A1:D10', 'Sheet1').
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_SHEETS_API}/{spreadsheet_id}/values/{range}",
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
            values = data.get("values", [])
            return {"status": "success", "range": data.get("range"), "rows": len(values), "values": values}
    except Exception as e:
        return {"status": "error", "message": f"Sheets API error: {e}"}


async def sheets_write(
    spreadsheet_id: str,
    range: str,
    values: list[list[str]],
    tool_context: ToolContext = None,
) -> dict:
    """Write data to a Google Spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet ID.
        range: Cell range in A1 notation (e.g., 'Sheet1!A1').
        values: 2D list of values to write (e.g., [["Name", "Sales"], ["Product A", 100]]).
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(
                f"{_SHEETS_API}/{spreadsheet_id}/values/{range}",
                params={"valueInputOption": "USER_ENTERED"},
                json={"values": values},
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
            return {"status": "success", "updated_range": data.get("updatedRange"), "updated_cells": data.get("updatedCells")}
    except Exception as e:
        return {"status": "error", "message": f"Sheets API error: {e}"}


async def sheets_create(
    title: str,
    tool_context: ToolContext = None,
) -> dict:
    """Create a new Google Spreadsheet.

    Args:
        title: The title for the new spreadsheet.

    Returns:
        dict: Spreadsheet ID and URL.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _SHEETS_API,
                json={"properties": {"title": title}},
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "status": "success",
                "spreadsheet_id": data["spreadsheetId"],
                "url": data["spreadsheetUrl"],
                "title": title,
            }
    except Exception as e:
        return {"status": "error", "message": f"Sheets create error: {e}"}
