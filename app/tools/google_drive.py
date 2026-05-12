"""Google Drive and Sheets tools — per-user OAuth2 access.

Each tool resolves the current user's email from session state,
retrieves their OAuth2 token, and makes authenticated API calls.
"""

import logging
import mimetypes
import os
import re
from typing import Optional

import httpx
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from app.app_utils.file_convert import is_convertible, to_text
from app.app_utils.tmp_sweeper import sweep_tmp
from app.tools.google_oauth.web_flow import refresh_access_token, start_auth_flow
from app.tools.google_oauth.token_store import (
    get_token, save_token, delete_token,
    save_user_mapping, resolve_email, delete_user_mapping,
)


# ---------------------------------------------------------------------------
# Drive ID extraction — accept URLs or bare IDs from the agent.
# ---------------------------------------------------------------------------
# Google Drive IDs are 25-80 char random strings (alphanum + `-_`). LLMs
# hallucinate when retyping them: g→q, 9→0, etc. (production proof
# 2026-05-12 — agent emitted `LQq…` for `LQg…` after the user pasted the
# real URL one message earlier).
#
# Every ID-taking tool below accepts a URL too. Helper extracts the
# canonical ID from any Google URL shape: Sheets, Docs, Slides,
# Drawings, Forms, generic Drive files, folders, or the `?id=` open
# URL. Bare IDs pass through. Anything else returns a structured error
# with a usage hint.

# Matches /<kind>/d/<id>, /folders/<id>, or `?id=<id>` / `&id=<id>`.
_GOOGLE_URL_ID_RE = re.compile(
    r"(?:"
    r"/(?:spreadsheets|document|presentation|forms|drawings|file)/d/"
    r"|/folders/"
    r"|[?&]id="
    r")([A-Za-z0-9_-]{20,80})"
)
# Bare ID — same charset, length range that real Drive IDs sit in.
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")


def _extract_drive_id(url_or_id: str) -> str:
    """Return the canonical Drive ID from a URL or bare ID. Raises ValueError otherwise.

    Accepts:
      - Bare 25-80 char ID
      - Sheets:        https://docs.google.com/spreadsheets/d/<ID>/edit?...
      - Docs:          https://docs.google.com/document/d/<ID>/edit?...
      - Slides:        https://docs.google.com/presentation/d/<ID>/edit?...
      - Forms:         https://docs.google.com/forms/d/<ID>/edit?...
      - Drawings:      https://docs.google.com/drawings/d/<ID>/edit?...
      - Drive file:    https://drive.google.com/file/d/<ID>/view?...
      - Drive folder:  https://drive.google.com/drive/folders/<ID>?...
      - Open URL:      https://drive.google.com/open?id=<ID>
      - URLs with `?pli=1`, `#gid=...`, `/u/0/`, etc. — trailing junk ignored
    """
    if not isinstance(url_or_id, str) or not url_or_id.strip():
        raise ValueError("expected a Google Drive URL or file ID, got empty input")
    s = url_or_id.strip()

    # Cheap path — bare ID
    if _BARE_ID_RE.fullmatch(s):
        return s

    # URL path
    m = _GOOGLE_URL_ID_RE.search(s)
    if m:
        return m.group(1)

    raise ValueError(
        f"Could not extract a Google Drive ID from {s[:120]!r}. "
        "Pass either a 25-80 char ID or a full Google URL "
        "(spreadsheets/d/, document/d/, presentation/d/, drawings/d/, "
        "forms/d/, file/d/, drive/folders/, or /open?id=)."
    )

# Gemini inline-data acceptance — mirrors the gate in load_artifacts_tool.
_ARTIFACT_INLINE_PREFIXES = ("image/", "audio/", "video/")
_ARTIFACT_INLINE_EXACT = {"application/pdf"}


def _is_artifact_inline(mime: str) -> bool:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    return mime.startswith(_ARTIFACT_INLINE_PREFIXES) or mime in _ARTIFACT_INLINE_EXACT

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
        folder_id: Optional folder ID OR a Drive folder URL
                   (https://drive.google.com/drive/folders/<ID> works too).
        max_results: Maximum number of results (default 20).
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    resolved_folder_id = ""
    if folder_id:
        try:
            resolved_folder_id = _extract_drive_id(folder_id)
        except ValueError as e:
            return {"status": "error", "message": str(e)}

    q_parts = []
    if query:
        q_parts.append(query)
    if resolved_folder_id:
        q_parts.append(f"'{resolved_folder_id}' in parents")
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


# Export MIME + extension for each Google Workspace type. Text-based exports
# (Docs → text/plain, Sheets → text/csv, Slides → text/plain) flow through
# file_convert.to_text so the agent sees content directly in the tool response.
# Drawings and everything else fall back to PDF.
_GOOGLE_EXPORT = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}
_GOOGLE_EXPORT_FALLBACK = ("application/pdf", ".pdf")


def _export_mime_and_ext(google_mime: str) -> tuple[str, str]:
    return _GOOGLE_EXPORT.get(google_mime, _GOOGLE_EXPORT_FALLBACK)


def _safe_drive_name(name: str) -> str:
    """Sanitize a Drive file/doc title for safe use as a local filesystem path.

    Drive titles are metadata and can contain any character, including `/` and
    `\\` (common in meeting notes like 'Notes: "A/B 1:1"'). Filesystems treat
    those as path separators, so `open(f"./dir/{title}", "wb")` silently tries
    to write into a non-existent subdirectory. Replace separators rather than
    basename'ing so the full title is preserved in the saved filename.
    """
    cleaned = (name or "file").strip().replace("/", "_").replace("\\", "_")
    return cleaned or "file"


async def drive_download_file(
    file_id: str,
    tool_context: ToolContext = None,
) -> dict:
    """Download the content of a file from Google Drive.

    Google Docs/Slides export as text/plain, Sheets as CSV, Drawings as PNG —
    text-based exports come back with their content in `extracted_text` so the
    agent can read them directly. Other Workspace types fall back to PDF.
    Regular files download as-is; convertible MIMEs (xlsx/csv/docx/pptx/rtf/
    txt/md/json/yaml) also populate `extracted_text`.

    Args:
        file_id: The Google Drive file ID OR any Drive file URL
                 (https://docs.google.com/document/d/<ID>/edit,
                 https://drive.google.com/file/d/<ID>/view, etc.).
                 URL is preferred — pass the user's link directly, do not
                 retype the ID from memory (LLM hallucination risk on
                 44-char random strings).
    """
    try:
        file_id = _extract_drive_id(file_id)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    sweep_tmp()

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            meta_resp = await client.get(
                f"{_DRIVE_API}/files/{file_id}",
                params={"fields": "name,mimeType"},
                headers=_auth_headers(token),
            )
            meta_resp.raise_for_status()
            meta = meta_resp.json()
            source_mime = meta.get("mimeType", "")
            original_name = meta.get("name", "file")
            name = _safe_drive_name(original_name)

            if source_mime.startswith("application/vnd.google-apps."):
                export_mime, ext = _export_mime_and_ext(source_mime)
                resp = await client.get(
                    f"{_DRIVE_API}/files/{file_id}/export",
                    params={"mimeType": export_mime},
                    headers=_auth_headers(token),
                )
                effective_mime = export_mime
            else:
                resp = await client.get(
                    f"{_DRIVE_API}/files/{file_id}",
                    params={"alt": "media"},
                    headers=_auth_headers(token),
                )
                effective_mime = source_mime or (mimetypes.guess_type(name)[0] or "application/octet-stream")
                ext = "" if os.path.splitext(name)[1] else (mimetypes.guess_extension(effective_mime) or "")
            resp.raise_for_status()

            os.makedirs("./tmp/drive_downloads", exist_ok=True)
            path = os.path.join("./tmp/drive_downloads", f"{name}{ext}")
            with open(path, "wb") as f:
                f.write(resp.content)

            extracted_text = None
            if is_convertible(effective_mime):
                extracted_text = to_text(resp.content, effective_mime, name)

            artifact_name = None
            saved_filename = os.path.basename(path)
            if tool_context and _is_artifact_inline(effective_mime):
                part = types.Part.from_bytes(data=resp.content, mime_type=effective_mime)
                try:
                    await tool_context.save_artifact(saved_filename, part)
                    artifact_name = saved_filename
                except Exception as exc:
                    logger.warning("save_artifact failed for %s: %s", saved_filename, exc)

            return {
                "status": "success",
                "file_path": path,
                "filename": name,
                "mime": effective_mime,
                "source_mime": source_mime,
                "size_bytes": len(resp.content),
                "extracted_text": extracted_text,
                "artifact_name": artifact_name,
            }
    except Exception as e:
        return {"status": "error", "message": f"Drive download error: {e}"}


# ---------------------------------------------------------------------------
# Sheets tools
# ---------------------------------------------------------------------------

async def sheets_read(
    spreadsheet_id: str,
    range: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Read data from a Google Spreadsheet.

    Args:
        spreadsheet_id: Spreadsheet ID OR full Sheets URL
            (https://docs.google.com/spreadsheets/d/<ID>/edit?...).
            URL is preferred — pass the user's link directly, do NOT
            retype the ID from memory (LLM hallucination risk).
        range: Cell range in A1 notation (e.g., 'Sheet1!A1:D10',
            'My Tab Name'). Leave empty to auto-select the first tab
            (recommended when the tab name is unknown — Sheet1 is
            often missing on real-world spreadsheets).
    """
    try:
        spreadsheet_id = _extract_drive_id(spreadsheet_id)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Auto-resolve range when none given: fetch first tab title.
            if not range:
                meta = await client.get(
                    f"{_SHEETS_API}/{spreadsheet_id}",
                    params={"fields": "sheets.properties.title"},
                    headers=_auth_headers(token),
                )
                if meta.status_code == 200:
                    sheets = meta.json().get("sheets", [])
                    titles = [
                        s.get("properties", {}).get("title")
                        for s in sheets
                        if s.get("properties", {}).get("title")
                    ]
                    if titles:
                        range = titles[0]
                    else:
                        range = "Sheet1"
                elif meta.status_code == 404:
                    return {
                        "status": "error",
                        "message": (
                            f"Spreadsheet not found (404). spreadsheet_id={spreadsheet_id!r}. "
                            "Verify the ID matches the URL exactly. The user may have shared "
                            "a different ID — re-paste the full URL and pass it directly."
                        ),
                    }
                else:
                    range = "Sheet1"

            resp = await client.get(
                f"{_SHEETS_API}/{spreadsheet_id}/values/{range}",
                headers=_auth_headers(token),
            )
            if resp.status_code == 404:
                return {
                    "status": "error",
                    "message": (
                        f"Sheets 404 for spreadsheet_id={spreadsheet_id!r}, range={range!r}. "
                        "Verify the ID matches the URL exactly — re-paste the full URL."
                    ),
                }
            if resp.status_code == 400:
                # Likely bad range — list real tabs so the agent can pick one.
                tabs_resp = await client.get(
                    f"{_SHEETS_API}/{spreadsheet_id}",
                    params={"fields": "sheets.properties.title"},
                    headers=_auth_headers(token),
                )
                tab_titles = []
                if tabs_resp.status_code == 200:
                    tab_titles = [
                        s.get("properties", {}).get("title")
                        for s in tabs_resp.json().get("sheets", [])
                        if s.get("properties", {}).get("title")
                    ]
                hint = (
                    f"Available tabs: {tab_titles}. Pass one as `range`."
                    if tab_titles
                    else "Could not list tabs either — check spreadsheet access."
                )
                return {
                    "status": "error",
                    "message": (
                        f"Sheets 400 for spreadsheet_id={spreadsheet_id!r}, "
                        f"range={range!r}: {resp.text[:200]}. {hint}"
                    ),
                }
            resp.raise_for_status()
            data = resp.json()
            values = data.get("values", [])
            return {"status": "success", "range": data.get("range"), "rows": len(values), "values": values}
    except Exception as e:
        return {
            "status": "error",
            "message": (
                f"Sheets API error for spreadsheet_id={spreadsheet_id!r}, "
                f"range={range!r}: {e}"
            ),
        }


async def sheets_write(
    spreadsheet_id: str,
    range: str,
    values: list[list[str]],
    tool_context: ToolContext = None,
) -> dict:
    """Write data to a Google Spreadsheet.

    Args:
        spreadsheet_id: Spreadsheet ID OR full Sheets URL. URL preferred.
        range: Cell range in A1 notation (e.g., 'Sheet1!A1').
        values: 2D list of values to write (e.g., [["Name", "Sales"], ["Product A", 100]]).
    """
    try:
        spreadsheet_id = _extract_drive_id(spreadsheet_id)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

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
            if resp.status_code in (400, 404):
                return {
                    "status": "error",
                    "message": (
                        f"Sheets {resp.status_code} for spreadsheet_id={spreadsheet_id!r}, "
                        f"range={range!r}: {resp.text[:200]}. "
                        "Verify the URL matches the user's link and the tab name is correct."
                    ),
                }
            resp.raise_for_status()
            data = resp.json()
            return {"status": "success", "updated_range": data.get("updatedRange"), "updated_cells": data.get("updatedCells")}
    except Exception as e:
        return {
            "status": "error",
            "message": (
                f"Sheets API error for spreadsheet_id={spreadsheet_id!r}, "
                f"range={range!r}: {e}"
            ),
        }


async def sheets_list_tabs(
    spreadsheet_id: str,
    tool_context: ToolContext = None,
) -> dict:
    """List every tab (worksheet) in a Google Spreadsheet.

    Use BEFORE `sheets_read` when the tab name is unknown — many real
    spreadsheets do NOT have a tab named 'Sheet1', so the default range
    fails with a 400.

    Args:
        spreadsheet_id: Spreadsheet ID OR full Sheets URL. URL preferred.

    Returns:
        dict with `tabs`: list of tab titles (in sheet order), plus
        `spreadsheet_id` of the resolved sheet.
    """
    try:
        spreadsheet_id = _extract_drive_id(spreadsheet_id)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_SHEETS_API}/{spreadsheet_id}",
                params={"fields": "sheets.properties.title,properties.title"},
                headers=_auth_headers(token),
            )
            if resp.status_code == 404:
                return {
                    "status": "error",
                    "message": (
                        f"Spreadsheet not found (404). spreadsheet_id={spreadsheet_id!r}. "
                        "Verify the ID matches the URL exactly — re-paste the full URL."
                    ),
                }
            resp.raise_for_status()
            data = resp.json()
            tabs = [
                s.get("properties", {}).get("title")
                for s in data.get("sheets", [])
                if s.get("properties", {}).get("title")
            ]
            return {
                "status": "success",
                "spreadsheet_id": spreadsheet_id,
                "title": data.get("properties", {}).get("title", ""),
                "tabs": tabs,
            }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Sheets API error for spreadsheet_id={spreadsheet_id!r}: {e}",
        }


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
