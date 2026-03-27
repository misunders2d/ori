"""
Module to handle manual session refresh signals between tools and the runner loop.
"""

_pending_refresh = {}  # session_id -> mode ('fresh' or 'summarize')

def request_refresh(session_id: str, mode: str):
    """Signals that the session should be refreshed after the current turn."""
    _pending_refresh[session_id] = mode

def get_pending_refresh(session_id: str) -> str | None:
    """Checks if a refresh was requested for the session."""
    return _pending_refresh.pop(session_id, None)
