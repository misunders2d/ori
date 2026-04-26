"""Google Calendar tools — per-user OAuth2 access.

Uses the same token store and auth flow as Drive/Sheets.
Each tool resolves the current user's Google email and uses their token.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx
from google.adk.tools.tool_context import ToolContext

from app.tools.google_drive import _auth_headers, _get_user_email, _get_valid_token

logger = logging.getLogger(__name__)

_CALENDAR_API = "https://www.googleapis.com/calendar/v3"


# ---------------------------------------------------------------------------
# Calendar discovery
# ---------------------------------------------------------------------------

async def calendar_list(
    tool_context: ToolContext = None,
) -> dict:
    """List all calendars the user has access to.

    Returns calendar IDs, names, and access roles. Use the calendar ID
    in other calendar tools (default 'primary' = user's main calendar).
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_CALENDAR_API}/users/me/calendarList",
                params={"minAccessRole": "reader"},
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
            calendars = [
                {
                    "id": c["id"],
                    "name": c.get("summary", ""),
                    "description": c.get("description", ""),
                    "primary": c.get("primary", False),
                    "access_role": c.get("accessRole", ""),
                    "time_zone": c.get("timeZone", ""),
                }
                for c in data.get("items", [])
            ]
            return {"status": "success", "count": len(calendars), "calendars": calendars}
    except Exception as e:
        return {"status": "error", "message": f"Calendar API error: {e}"}


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

async def calendar_list_events(
    days: int = 7,
    calendar_id: str = "primary",
    max_results: int = 25,
    query: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """List upcoming calendar events.

    Args:
        days: How many days ahead to look (default 7).
        calendar_id: Calendar ID (default 'primary' = user's main calendar).
                     Use calendar_list to discover other calendars.
        max_results: Maximum events to return (default 25).
        query: Optional text search within event titles and descriptions.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    now = datetime.now(timezone.utc)
    time_max = now + timedelta(days=max(days, 1))

    params = {
        "timeMin": now.isoformat(),
        "timeMax": time_max.isoformat(),
        "maxResults": min(max_results, 250),
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if query:
        params["q"] = query

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events",
                params=params,
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
            events = [
                {
                    "id": e["id"],
                    "title": e.get("summary", "(no title)"),
                    "start": e.get("start", {}).get("dateTime") or e.get("start", {}).get("date", ""),
                    "end": e.get("end", {}).get("dateTime") or e.get("end", {}).get("date", ""),
                    "location": e.get("location", ""),
                    "description": (e.get("description", "") or "")[:200],
                    "status": e.get("status", ""),
                    "html_link": e.get("htmlLink", ""),
                    "attendees": [
                        {"email": a.get("email", ""), "response": a.get("responseStatus", "")}
                        for a in e.get("attendees", [])
                    ] if e.get("attendees") else [],
                }
                for e in data.get("items", [])
            ]
            return {"status": "success", "count": len(events), "events": events}
    except Exception as e:
        return {"status": "error", "message": f"Calendar API error: {e}"}


async def calendar_create_event(
    title: str,
    start_datetime: str,
    end_datetime: str,
    description: str = "",
    location: str = "",
    attendees: str = "",
    calendar_id: str = "primary",
    timezone_str: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Create a new calendar event.

    Args:
        title: Event title/summary.
        start_datetime: Start time in ISO 8601 format (e.g. '2026-04-10T10:00:00').
        end_datetime: End time in ISO 8601 format (e.g. '2026-04-10T11:00:00').
        description: Optional event description/notes.
        location: Optional location (address or virtual meeting link).
        attendees: Optional comma-separated email addresses to invite.
        calendar_id: Calendar ID (default 'primary').
        timezone_str: IANA timezone (e.g. 'America/New_York'). If empty, uses the calendar's default.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    if not title or not start_datetime or not end_datetime:
        return {"status": "error", "message": "title, start_datetime, and end_datetime are all required."}

    event_body = {
        "summary": title,
        "start": {"dateTime": start_datetime},
        "end": {"dateTime": end_datetime},
    }

    if timezone_str:
        event_body["start"]["timeZone"] = timezone_str
        event_body["end"]["timeZone"] = timezone_str

    if description:
        event_body["description"] = description
    if location:
        event_body["location"] = location
    if attendees:
        event_body["attendees"] = [
            {"email": a.strip()} for a in attendees.split(",") if a.strip()
        ]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events",
                json=event_body,
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "status": "success",
                "event_id": data["id"],
                "html_link": data.get("htmlLink", ""),
                "message": f"Event '{title}' created.",
            }
    except Exception as e:
        return {"status": "error", "message": f"Calendar API error: {e}"}


async def calendar_update_event(
    event_id: str,
    title: str = "",
    start_datetime: str = "",
    end_datetime: str = "",
    description: str = "",
    location: str = "",
    attendees: str = "",
    calendar_id: str = "primary",
    timezone_str: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Update an existing calendar event. Only provided fields are changed.

    Args:
        event_id: The event ID to update (from calendar_list_events).
        title: New title (leave empty to keep current).
        start_datetime: New start time in ISO 8601 format.
        end_datetime: New end time in ISO 8601 format.
        description: New description.
        location: New location.
        attendees: Comma-separated email addresses (replaces current attendees).
        calendar_id: Calendar ID (default 'primary').
        timezone_str: IANA timezone for start/end times.
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    if not event_id:
        return {"status": "error", "message": "event_id is required."}

    # Build patch body — only include provided fields
    patch = {}
    if title:
        patch["summary"] = title
    if start_datetime:
        start = {"dateTime": start_datetime}
        if timezone_str:
            start["timeZone"] = timezone_str
        patch["start"] = start
    if end_datetime:
        end = {"dateTime": end_datetime}
        if timezone_str:
            end["timeZone"] = timezone_str
        patch["end"] = end
    if description:
        patch["description"] = description
    if location:
        patch["location"] = location
    if attendees:
        patch["attendees"] = [
            {"email": a.strip()} for a in attendees.split(",") if a.strip()
        ]

    if not patch:
        return {"status": "error", "message": "No fields to update. Provide at least one field."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                json=patch,
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "status": "success",
                "event_id": data["id"],
                "message": f"Event updated: {data.get('summary', event_id)}",
            }
    except Exception as e:
        return {"status": "error", "message": f"Calendar API error: {e}"}


async def calendar_delete_event(
    event_id: str,
    calendar_id: str = "primary",
    tool_context: ToolContext = None,
) -> dict:
    """Delete a calendar event.

    Args:
        event_id: The event ID to delete.
        calendar_id: Calendar ID (default 'primary').
    """
    email = _get_user_email(tool_context)
    token = await _get_valid_token(email)
    if not token:
        return {"status": "error", "message": f"Google not connected for {email}. Use google_connect first."}

    if not event_id:
        return {"status": "error", "message": "event_id is required."}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"{_CALENDAR_API}/calendars/{calendar_id}/events/{event_id}",
                headers=_auth_headers(token),
            )
            resp.raise_for_status()
            return {"status": "success", "message": f"Event {event_id} deleted."}
    except Exception as e:
        return {"status": "error", "message": f"Calendar API error: {e}"}
