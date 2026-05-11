"""Session-scoped scratchpad for multi-step research tasks.

Files live in `tmp/scratchpads/{session_id}/{owner}__{name}.md` and are
cleaned up on session reset. Stays OUT of LLM context — the agent reads
explicitly when it needs to synthesize.

`owner` is the agent that wrote the pad (`tool_context.agent.name` —
e.g. `AmazonAgent`, `CoordinatorAgent`). Filenames are tagged with
owner so cross-agent reads in the same session can disambiguate. Files
written before owner tagging existed (legacy `{name}.md`) remain
readable, writeable, and deletable — the helpers try the owner-tagged
path first and fall back to the bare name.

Lifecycle:
- write/read/replace/clear: all session-scoped.
- cleanup_session_scratchpads: nukes the whole `{session_id}/` on session
  reset. Called from `_perform_session_refresh` in app/core/agent_executor.py.
- TTL sweep: `sweep_scratchpad_sessions` in
  app/app_utils/tmp_sweeper.py removes session dirs untouched for
  `SCRATCHPAD_SESSION_TTL_DAYS` (default 7).
"""

import json
import logging
import os
import re
import shutil

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_SCRATCHPAD_DIR = os.path.abspath("./tmp/scratchpads")

# Owner names are agent class names — alphanumeric + underscore.
_OWNER_SAFE = re.compile(r"[^a-zA-Z0-9_]")


def _sanitize_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)


def _sanitize_owner(owner) -> str:
    # Tolerate non-string inputs gracefully. Mocked tool_contexts in tests
    # have `_invocation_context.agent.name` return a `MagicMock`, not a
    # str — without this guard the call to `.strip()` raises and breaks
    # every scratchpad write that flows through `_detect_owner`.
    if not isinstance(owner, str):
        return ""
    return _OWNER_SAFE.sub("_", owner.strip()) if owner else ""


def _session_id(tool_context: ToolContext | None) -> str:
    session = getattr(tool_context, "session", None) if tool_context else None
    return (
        getattr(session, "session_id", None)
        or getattr(session, "id", None)
        or "default"
    )


def _detect_owner(tool_context: ToolContext | None) -> str:
    """Best-effort: pull the active agent name out of the tool context.

    ADK 1.x exposes it via `tool_context._invocation_context.agent.name` on
    the LlmAgent path. Older shapes may expose `tool_context.agent.name`.
    Returns empty string when we can't tell — caller treats this as
    "legacy untagged write" and skips the owner prefix.
    """
    if not tool_context:
        return ""
    # New path (LlmAgent + invocation_context).
    inv = getattr(tool_context, "_invocation_context", None)
    agent = getattr(inv, "agent", None) if inv is not None else None
    name = getattr(agent, "name", "") if agent is not None else ""
    if not name:
        # Direct exposure on the tool_context itself.
        agent = getattr(tool_context, "agent", None)
        name = getattr(agent, "name", "") if agent is not None else ""
    return _sanitize_owner(name)


def _session_dir(session_id: str) -> str:
    return os.path.join(_SCRATCHPAD_DIR, session_id)


def _candidate_paths(session_id: str, name: str, owner: str) -> list[str]:
    """Paths to probe on a read (priority order) or to write (first entry).

    1. owner-tagged path (`{owner}__{name}.md`) — Phase 7 scheme
    2. legacy bare path (`{name}.md`) — backwards compat for old sessions

    When `owner` is empty (e.g. caller didn't pass one and we couldn't
    detect it), only the legacy bare path is returned.
    """
    base = _session_dir(session_id)
    safe_name = _sanitize_name(name)
    paths: list[str] = []
    if owner:
        paths.append(os.path.join(base, f"{owner}__{safe_name}.md"))
    paths.append(os.path.join(base, f"{safe_name}.md"))
    return paths


def _find_existing(session_id: str, name: str, owner: str) -> str | None:
    """Return the first existing candidate path, or None."""
    for path in _candidate_paths(session_id, name, owner):
        if os.path.isfile(path):
            return path
    return None


def _parse_filename(filename: str) -> tuple[str, str]:
    """Split `{owner}__{name}.md` back into (owner, name).

    Legacy files (`{name}.md` only) return (`""`, name).
    """
    stem = filename[:-3] if filename.endswith(".md") else filename
    if "__" in stem:
        owner, name = stem.split("__", 1)
        return owner, name
    return "", stem


def scratchpad_write(
    name: str,
    content: str,
    tool_context: ToolContext = None,
    owner: str = "",
) -> dict:
    """Append content to a named scratchpad. Creates it if it doesn't exist.

    Use this to record intermediate findings, partial results, or raw data
    during multi-step research tasks. Data is NOT added to conversation context —
    call scratchpad_read when you need to review or summarize.

    Args:
        name: Short name for the scratchpad (e.g., 'competitor-research', 'price-analysis').
        content: Text to append.
        owner: Optional override for the writing agent's name. If omitted,
            the active agent is detected from `tool_context`. Used to
            disambiguate pads with the same `name` written by different
            sub-agents in the same session.

    Returns:
        dict: Status, scratchpad name, owner tag, current size.
    """
    sid = _session_id(tool_context)
    effective_owner = _sanitize_owner(owner) or _detect_owner(tool_context)
    paths = _candidate_paths(sid, name, effective_owner)

    # Append to an existing pad first if one exists (under any candidate
    # name), so an agent reading from an old session keeps writing to the
    # same file rather than fragmenting into a fresh one.
    existing = _find_existing(sid, name, effective_owner)
    target = existing if existing else paths[0]

    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "a") as f:
        f.write(content + "\n\n")

    size = os.path.getsize(target)
    return {
        "status": "success",
        "scratchpad": name,
        "owner": effective_owner,
        "size_bytes": size,
    }


def scratchpad_read(
    name: str,
    tool_context: ToolContext = None,
    owner: str = "",
) -> dict:
    """Read the full contents of a named scratchpad.

    Call this when you're ready to synthesize findings or produce a final answer.

    Args:
        name: The scratchpad name to read.
        owner: Optional — read a pad owned by a specific agent. If omitted,
            tries (detected owner) → (legacy bare) in order.

    Returns:
        dict: Status and scratchpad content.
    """
    sid = _session_id(tool_context)
    effective_owner = _sanitize_owner(owner) or _detect_owner(tool_context)
    path = _find_existing(sid, name, effective_owner)
    if path is None:
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


def scratchpad_replace(
    name: str,
    content: str,
    tool_context: ToolContext = None,
    owner: str = "",
) -> dict:
    """Replace the entire contents of a scratchpad. Useful for rewriting with a summary.

    Args:
        name: The scratchpad name.
        content: New content to replace with.
        owner: Optional — only replace a pad owned by a specific agent.

    Returns:
        dict: Status and new size.
    """
    sid = _session_id(tool_context)
    effective_owner = _sanitize_owner(owner) or _detect_owner(tool_context)
    existing = _find_existing(sid, name, effective_owner)
    target = existing if existing else _candidate_paths(sid, name, effective_owner)[0]

    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(content + "\n")

    return {
        "status": "success",
        "scratchpad": name,
        "owner": effective_owner,
        "size_bytes": len(content),
    }


def scratchpad_clear(
    name: str,
    tool_context: ToolContext = None,
    owner: str = "",
) -> dict:
    """Delete a scratchpad when the task is complete.

    Args:
        name: The scratchpad name to delete.
        owner: Optional — only delete the pad owned by a specific agent.

    Returns:
        dict: Status.
    """
    sid = _session_id(tool_context)
    effective_owner = _sanitize_owner(owner) or _detect_owner(tool_context)
    path = _find_existing(sid, name, effective_owner)
    if path is not None:
        os.remove(path)
    return {"status": "success", "message": f"Scratchpad '{name}' cleared."}


def scratchpad_list(
    tool_context: ToolContext = None,
    owner: str = "",
) -> dict:
    """List active scratchpads for the current session.

    Args:
        owner: Optional — filter to pads owned by a specific agent. Omit
            to list all pads in the session (any owner + legacy).

    Returns:
        dict: List of {name, owner, size_bytes} entries.
    """
    sid = _session_id(tool_context)
    sdir = _session_dir(sid)
    if not os.path.isdir(sdir):
        return {"status": "success", "scratchpads": []}

    owner_filter = _sanitize_owner(owner)
    pads = []
    for filename in sorted(os.listdir(sdir)):
        if not filename.endswith(".md"):
            continue
        file_owner, file_name = _parse_filename(filename)
        if owner_filter and file_owner != owner_filter:
            continue
        path = os.path.join(sdir, filename)
        pads.append({
            "name": file_name,
            "owner": file_owner,
            "size_bytes": os.path.getsize(path),
        })

    return {"status": "success", "scratchpads": pads}


def cleanup_session_scratchpads(session_id: str):
    """Delete all scratchpads for a session. Called during session reset."""
    sdir = _session_dir(session_id)
    if os.path.isdir(sdir):
        shutil.rmtree(sdir, ignore_errors=True)
        logger.info("Cleaned up scratchpads for session %s", session_id)
