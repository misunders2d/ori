"""
Module to handle manual session refresh signals between tools and the runner loop.
"""
import logging

logger = logging.getLogger(__name__)

_pending_refresh = {}  # session_id -> mode ('fresh' or 'summarize')

def request_refresh(session_id: str, mode: str):
    """Signals that the session should be refreshed after the current turn."""
    logger.info("SIGNAL: Requesting session refresh (%s) for session: %s", mode, session_id)
    _pending_refresh[session_id] = mode

def get_pending_refresh(session_id: str) -> str | None:
    """Checks if a refresh was requested for the session."""
    mode = _pending_refresh.pop(session_id, None)
    if mode:
        logger.info("SIGNAL: Retrieved pending refresh (%s) for session: %s", mode, session_id)
    return mode
