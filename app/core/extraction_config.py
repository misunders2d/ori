"""Opt-in extraction config — controls which sessions have auto entity extraction.

Only sessions explicitly listed in data/extraction_config.json will have
entities and relationships auto-extracted to the knowledge graph.
Everything else is excluded by default.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.abspath("./data/extraction_config.json")

_enabled_sessions: set[str] = set()


def _load():
    global _enabled_sessions
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, "r") as f:
                data = json.load(f)
                _enabled_sessions = set(data.get("enabled_sessions", []))
        except Exception as e:
            logger.warning("Failed to load extraction config: %s", e)
            _enabled_sessions = set()
    else:
        _enabled_sessions = set()


def _save():
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    with open(_CONFIG_PATH, "w") as f:
        json.dump({"enabled_sessions": sorted(_enabled_sessions)}, f, indent=2)


def is_extraction_enabled(session_id: str) -> bool:
    """Check if a session has auto entity extraction enabled."""
    return session_id in _enabled_sessions


def enable_extraction(session_id: str):
    """Enable auto entity extraction for a session."""
    _enabled_sessions.add(session_id)
    _save()
    logger.info("Entity extraction enabled for %s", session_id)


def disable_extraction(session_id: str):
    """Disable auto entity extraction for a session."""
    _enabled_sessions.discard(session_id)
    _save()
    logger.info("Entity extraction disabled for %s", session_id)


def list_extraction_sessions() -> list[str]:
    """Return all sessions with extraction enabled."""
    return sorted(_enabled_sessions)


# Bootstrap
_load()
