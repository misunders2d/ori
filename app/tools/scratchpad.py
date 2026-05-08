"""Session-scoped scratchpad for multi-step research tasks.

Files live in data/scratchpads/{session_id}/{name}.md and are cleaned
up on session reset. Stays OUT of LLM context — the agent reads
explicitly when it needs to synthesize.
"""

import logging
import json
import os
import shutil

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_SCRATCHPAD_DIR = os.path.abspath("./tmp/scratchpads")


def _pad_path(session_id: str, name: str) -> str:
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return os.path.join(_SCRATCHPAD_DIR, session_id, f"{safe_name}.md")


def _session_dir(session_id: str) -> str:
    return os.path.join(_SCRATCHPAD_DIR, session_id)


def scratchpad_write(name: str, content: str, tool_context: ToolContext = None) -> dict:
    """Append content to a named scratchpad. Creates it if it doesn't exist.

    Use this to record intermediate findings, partial results, or raw data
    during multi-step research tasks. Data is NOT added to conversation context —
    call scratchpad_read when you need to review or summarize.

    Args:
        name: Short name for the scratchpad (e.g., 'competitor-research', 'price-analysis').
        content: Text to append.

    Returns:
        dict: Status and current scratchpad size.
    """
    session = getattr(tool_context, "session", None) if tool_context else None
    session_id = getattr(session, "session_id", None) or getattr(session, "id", None) or "default"

    path = _pad_path(session_id, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "a") as f:
        f.write(content + "\n\n")

    size = os.path.getsize(path)
    return {"status": "success", "scratchpad": name, "size_bytes": size}


def scratchpad_read(name: str, tool_context: ToolContext = None) -> dict:
    """Read the full contents of a named scratchpad.

    Call this when you're ready to synthesize findings or produce a final answer.

    Args:
        name: The scratchpad name to read.

    Returns:
        dict: Status and scratchpad content.
    """
    session = getattr(tool_context, "session", None) if tool_context else None
    session_id = getattr(session, "session_id", None) or getattr(session, "id", None) or "default"

    path = _pad_path(session_id, name)
    if not os.path.exists(path):
        return {"status": "error", "message": f"Scratchpad '{name}' not found."}

    with open(path) as f:
        content = f.read()

    try:
        parsed = json.loads(content.strip())
        from app.core.tool_artifacts import inline_file_summaries, redact_inline_file_data

        file_payloads = inline_file_summaries(parsed)
        if file_payloads:
            redacted = redact_inline_file_data(parsed)
            return {
                "status": "success",
                "scratchpad": name,
                "content": json.dumps(redacted, default=str, ensure_ascii=False),
                "size_bytes": len(content),
                "redacted_inline_files": True,
                "file_payloads": file_payloads,
                "message": (
                    "Inline file bytes were redacted from the model-visible scratchpad_read "
                    "response. Transport delivery can still use the stored payload."
                ),
            }
    except Exception:
        pass

    return {"status": "success", "scratchpad": name, "content": content, "size_bytes": len(content)}


def scratchpad_replace(name: str, content: str, tool_context: ToolContext = None) -> dict:
    """Replace the entire contents of a scratchpad. Useful for rewriting with a summary.

    Args:
        name: The scratchpad name.
        content: New content to replace with.

    Returns:
        dict: Status and new size.
    """
    session = getattr(tool_context, "session", None) if tool_context else None
    session_id = getattr(session, "session_id", None) or getattr(session, "id", None) or "default"

    path = _pad_path(session_id, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w") as f:
        f.write(content + "\n")

    return {"status": "success", "scratchpad": name, "size_bytes": len(content)}


def scratchpad_clear(name: str, tool_context: ToolContext = None) -> dict:
    """Delete a scratchpad when the task is complete.

    Args:
        name: The scratchpad name to delete.

    Returns:
        dict: Status.
    """
    session = getattr(tool_context, "session", None) if tool_context else None
    session_id = getattr(session, "session_id", None) or getattr(session, "id", None) or "default"

    path = _pad_path(session_id, name)
    if os.path.exists(path):
        os.remove(path)
    return {"status": "success", "message": f"Scratchpad '{name}' cleared."}


def scratchpad_list(tool_context: ToolContext = None) -> dict:
    """List all active scratchpads for the current session.

    Returns:
        dict: List of scratchpad names and sizes.
    """
    session = getattr(tool_context, "session", None) if tool_context else None
    session_id = getattr(session, "session_id", None) or getattr(session, "id", None) or "default"

    sdir = _session_dir(session_id)
    if not os.path.isdir(sdir):
        return {"status": "success", "scratchpads": []}

    pads = []
    for f in sorted(os.listdir(sdir)):
        if f.endswith(".md"):
            path = os.path.join(sdir, f)
            pads.append({"name": f[:-3], "size_bytes": os.path.getsize(path)})

    return {"status": "success", "scratchpads": pads}


def cleanup_session_scratchpads(session_id: str):
    """Delete all scratchpads for a session. Called during session reset."""
    sdir = _session_dir(session_id)
    if os.path.isdir(sdir):
        shutil.rmtree(sdir, ignore_errors=True)
        logger.info("Cleaned up scratchpads for session %s", session_id)
