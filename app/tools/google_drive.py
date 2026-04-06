"""Google Drive and Sheets tools — per-user OAuth2 access.

Each tool resolves the current user's email from session state,
retrieves their OAuth2 token, and makes authenticated API calls.
"""

import logging
from typing import Optional

import httpx
from google.adk.tools.tool_context import ToolContext

from app.tools.google_oauth.device_flow import refresh_access_token, start_device_flow, poll_for_token
from app.tools.google_oauth.token_store import get_token, save_token, delete_token

logger = logging.getLogger(__name__)

_DRIVE_API = "https://www.googleapis.com/drive/v3"
_SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"


def _get_user_email(tool_context: ToolContext) -> str:
    """Extract user email from session state."""
    if not tool_context:
        return ""
    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    return state.get("user_id", "")  # user_id is email for Slack users


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
    """Start Google Drive/Sheets authorization for the current user.

    Generates a URL and code. The user opens the URL in a browser and enters
    the code to grant access. Then call google_connect_complete to finish.

    Returns:
        dict: URL and code for the user, plus a device_code for the completion step.
    """
    result = await start_device_flow()
    if result["status"] != "success":
        return result

    # Store device_code in session state so the completion tool can find it
    if tool_context:
        tool_context.state["_google_device_code"] = result["device_code"]
        tool_context.state["_google_poll_interval"] = result["interval"]

    return {
        "status": "success",
        "message": (
            f"Open this URL and enter the code:\n\n"
            f"**URL:** {result['verification_url']}\n"
            f"**Code:** `{result['user_code']}`\n\n"
            f"After you've authorized, tell me and I'll complete the connection."
        ),
    }


async def google_connect_complete(tool_context: ToolContext = None) -> dict:
    """Complete the Google authorization after the user has entered the code.

    Call this after the user confirms they've authorized in the browser.

    Returns:
        dict: Success with user email, or error.
    """
    if not tool_context:
        return {"status": "error", "message": "No session context available."}

    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    device_code = state.get("_google_device_code")
    interval = state.get("_google_poll_interval", 5)

    if not device_code:
        return {"status": "error", "message": "No pending authorization. Call google_connect first."}

    result = await poll_for_token(device_code, interval=interval, timeout=60)
    if result["status"] != "success":
        return result

    email = result["email"]
    from app.tools.google_oauth.device_flow import SCOPES
    save_token(email, result["access_token"], result["refresh_token"], result["expires_in"], SCOPES)

    # Clean up session state
    tool_context.state["_google_device_code"] = None
    tool_context.state["_google_poll_interval"] = None

    return {
        "status": "success",
        "message": f"Google Drive/Sheets connected for **{email}**. You can now use Drive and Sheets tools.",
    }


async def google_disconnect(tool_context: ToolContext = None) -> dict:
    """Disconnect Google Drive/Sheets for the current user. Removes stored tokens."""
    email = _get_user_email(tool_context)
    if not email:
        return {"status": "error", "message": "Could not determine user."}
    delete_token(email)
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
