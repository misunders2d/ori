"""User preferences storage.

Each user gets a plain Markdown file at ``./data/preferences/{user_id}.md``.
The content is loaded into session state on every turn by ``state_setter``
and made available to the agent instruction via the ``{user_preferences}`` key.
"""

import os

from google.adk.tools.tool_context import ToolContext

PREFERENCES_DIR = os.path.abspath("./data/preferences")


def _prefs_path(user_id: str) -> str:
    safe_id = user_id.replace("/", "_").replace("..", "_")
    return os.path.join(PREFERENCES_DIR, f"{safe_id}.md")


def load_user_preferences(user_id: str) -> str:
    """Read preferences from disk. Returns empty string if none exist."""
    path = _prefs_path(user_id)
    if os.path.isfile(path):
        with open(path) as f:
            return f.read().strip()
    return ""


def save_user_preferences(preferences: str, tool_context: ToolContext) -> dict:
    """Save or update the current user's preferences.

    Call this when the user tells you how they like to interact — communication
    style, language, topics of interest, recurring instructions, or anything
    they want you to always remember.

    Args:
        preferences: The full updated preferences text in Markdown format.

    Returns:
        dict: Status of the save operation.
    """
    user_id = tool_context.state.get("user_id", "")
    if not user_id:
        return {"status": "error", "message": "Could not determine user ID."}

    os.makedirs(PREFERENCES_DIR, exist_ok=True)
    path = _prefs_path(user_id)

    with open(path, "w") as f:
        f.write(preferences)

    # Update session state immediately so the agent sees it this turn
    tool_context.state["user_preferences"] = preferences

    return {
        "status": "success",
        "message": "Preferences saved. I'll use these going forward.",
    }


def get_user_preferences(tool_context: ToolContext) -> dict:
    """Retrieve the current user's saved preferences.

    Returns:
        dict: The preferences text, or a message if none are saved.
    """
    user_id = tool_context.state.get("user_id", "")
    if not user_id:
        return {"status": "error", "message": "Could not determine user ID."}

    prefs = load_user_preferences(user_id)
    if prefs:
        return {"status": "success", "preferences": prefs}
    return {"status": "success", "preferences": "", "message": "No preferences saved yet."}
