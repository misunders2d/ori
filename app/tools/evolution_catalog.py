"""Evolution catalog — verified evolution library for sharing across instances.

Each evolution is a self-contained folder in evolutions/{name}/ containing:
  - EVOLUTION.md: metadata + usage docs
  - Code files mirroring project structure (e.g. app/tools/youtube.py)

Evolutions are cataloged after successful verification, searchable locally,
and shareable with A2A friends.
"""

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Optional

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EVOLUTIONS_DIR = os.path.join(PROJECT_ROOT, "evolutions")


def _safe_resolve_path(file_path: str, base_dir: str) -> str | None:
    base = os.path.abspath(base_dir)
    resolved = os.path.abspath(os.path.join(base, file_path))
    if resolved != base and not resolved.startswith(base + os.sep):
        return None
    return resolved


def evolution_catalog(
    name: str,
    description: str,
    file_paths: list[str],
    tags: str = "",
    usage_notes: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Catalog a verified evolution into the evolutions library.

    Call this AFTER a successful evolution commit. It copies the live files
    into evolutions/{name}/ with an EVOLUTION.md manifest.

    Args:
        name: Short, meaningful name (e.g. 'youtube-integration', 'slack-adapter').
            Used as folder name — lowercase, hyphens, no spaces.
        description: What this evolution does (1-2 sentences).
        file_paths: List of relative file paths to include (e.g. ['app/tools/youtube.py']).
        tags: Comma-separated tags for search (e.g. 'youtube,video,google-api').
        usage_notes: Optional usage instructions or examples.

    Returns:
        dict: Status and path of the cataloged evolution.
    """
    safe_name = name.strip().lower().replace(" ", "-")
    if not safe_name:
        return {"status": "error", "message": "Name is required."}

    evo_dir = os.path.join(EVOLUTIONS_DIR, safe_name)
    if os.path.exists(evo_dir):
        return {"status": "error", "message": f"Evolution '{safe_name}' already exists. Use a different name or remove it first."}

    # Validate all files exist
    missing = []
    for fp in file_paths:
        full = os.path.join(PROJECT_ROOT, fp)
        if not os.path.isfile(full):
            missing.append(fp)
    if missing:
        return {"status": "error", "message": f"Files not found: {', '.join(missing)}"}

    os.makedirs(evo_dir, exist_ok=True)

    # Copy files preserving relative structure
    copied = []
    for fp in file_paths:
        src = os.path.join(PROJECT_ROOT, fp)
        dst = os.path.join(evo_dir, fp)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(fp)

    # Generate EVOLUTION.md
    bot_name = os.environ.get("BOT_NAME", "Ori")
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    md_lines = [
        "---",
        f"name: {safe_name}",
        f"description: {description}",
        f"author: {bot_name}",
        f"created: {now}",
        f"verified: true",
        f"tags: [{', '.join(tag_list)}]",
        "files:",
    ]
    for fp in copied:
        md_lines.append(f"  - {fp}")
    md_lines.append("---")
    md_lines.append("")
    md_lines.append(f"# {name}")
    md_lines.append("")
    md_lines.append(description)
    md_lines.append("")

    if usage_notes:
        md_lines.append("## Usage")
        md_lines.append("")
        md_lines.append(usage_notes)
        md_lines.append("")

    md_lines.append("## Files")
    md_lines.append("")
    for fp in copied:
        md_lines.append(f"- `{fp}`")
    md_lines.append("")

    with open(os.path.join(evo_dir, "EVOLUTION.md"), "w") as f:
        f.write("\n".join(md_lines))

    return {
        "status": "success",
        "message": f"Evolution '{safe_name}' cataloged with {len(copied)} file(s).",
        "path": f"evolutions/{safe_name}/",
        "files": copied,
    }


def evolution_search(
    query: str,
    tool_context: ToolContext = None,
) -> dict:
    """Search the local evolutions library by keyword.

    Searches evolution names, descriptions, and tags.

    Args:
        query: Search keyword (e.g. 'youtube', 'slack', 'database').

    Returns:
        dict: Matching evolutions with their metadata.
    """
    query_lower = query.lower().strip()
    if not query_lower:
        return {"status": "error", "message": "Search query is required."}

    results = []
    if not os.path.isdir(EVOLUTIONS_DIR):
        return {"status": "success", "results": [], "message": "No evolutions cataloged yet."}

    for name in sorted(os.listdir(EVOLUTIONS_DIR)):
        evo_dir = os.path.join(EVOLUTIONS_DIR, name)
        md_path = os.path.join(evo_dir, "EVOLUTION.md")
        if not os.path.isfile(md_path):
            continue

        with open(md_path) as f:
            content = f.read()

        # Simple keyword match across the whole manifest
        if query_lower in content.lower() or query_lower in name.lower():
            # Extract metadata from frontmatter
            meta = _parse_frontmatter(content)
            meta["name"] = name
            results.append(meta)

    return {
        "status": "success",
        "query": query,
        "results": results,
        "count": len(results),
    }


def evolution_share(
    name: str,
    tool_context: ToolContext = None,
) -> dict:
    """Package a local evolution for sharing via A2A.

    Returns the evolution's files and metadata as a shareable payload.
    Use this when a friend requests an evolution you have.

    Args:
        name: Evolution name (folder name in evolutions/).

    Returns:
        dict: Shareable payload with metadata and file contents.
    """
    evo_dir = os.path.join(EVOLUTIONS_DIR, name)
    if not os.path.isdir(evo_dir):
        return {"status": "error", "message": f"Evolution '{name}' not found."}

    md_path = os.path.join(evo_dir, "EVOLUTION.md")
    if not os.path.isfile(md_path):
        return {"status": "error", "message": f"Evolution '{name}' has no EVOLUTION.md."}

    with open(md_path) as f:
        manifest = f.read()

    # Collect all files (excluding EVOLUTION.md itself)
    files = {}
    for root, _dirs, filenames in os.walk(evo_dir):
        for fname in filenames:
            if fname == "EVOLUTION.md":
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, evo_dir)
            with open(full) as f:
                files[rel] = f.read()

    return {
        "status": "success",
        "name": name,
        "manifest": manifest,
        "files": files,
    }


def evolution_import(
    name: str,
    manifest: str,
    files: dict[str, str],
    apply: bool = False,
    tool_context: ToolContext = None,
) -> dict:
    """Import an evolution received from an A2A friend.

    Saves it to the local evolutions library. Imported code is never applied
    directly to the live project tree; it must go through sandbox verification.

    Args:
        name: Evolution name.
        manifest: The EVOLUTION.md content.
        files: Dict of {relative_path: file_content}.
        apply: Deprecated and blocked. Imported code must be staged and verified.

    Returns:
        dict: Status and what was imported.
    """
    if apply:
        return {
            "status": "error",
            "message": (
                "Direct live apply is blocked. Import the evolution, inspect it, "
                "stage selected files through evolution_stage_change, verify, then commit."
            ),
        }

    safe_name = name.strip().lower().replace(" ", "-")
    evo_dir = os.path.join(EVOLUTIONS_DIR, safe_name)

    if os.path.exists(evo_dir):
        return {"status": "error", "message": f"Evolution '{safe_name}' already exists locally. Remove it first to re-import."}

    resolved_files = []
    for rel_path, content in files.items():
        dst = _safe_resolve_path(rel_path, evo_dir)
        if dst is None:
            return {
                "status": "error",
                "message": f"Path traversal denied for imported file: {rel_path}",
            }
        resolved_files.append((rel_path, content, dst))

    os.makedirs(evo_dir, exist_ok=True)

    # Write manifest
    with open(os.path.join(evo_dir, "EVOLUTION.md"), "w") as f:
        f.write(manifest)

    # Write files
    written = []
    for rel_path, content, dst in resolved_files:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w") as f:
            f.write(content)
        written.append(rel_path)

    applied = []
    msg = f"Evolution '{safe_name}' imported with {len(written)} file(s)."

    return {
        "status": "success",
        "message": msg,
        "path": f"evolutions/{safe_name}/",
        "files": written,
        "applied": applied,
    }


def _parse_frontmatter(content: str) -> dict:
    """Extract YAML-like frontmatter from EVOLUTION.md."""
    meta = {}
    if not content.startswith("---"):
        return meta
    parts = content.split("---", 2)
    if len(parts) < 3:
        return meta
    for line in parts[1].strip().split("\n"):
        if ":" in line and not line.startswith(" "):
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta
