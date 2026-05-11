#!/usr/bin/env python3
"""AST-scan symbol indexer for Ori.

Walks the agent / tool / toolset / callback / skill directories, parses each
file (no imports — AST only, safe in restricted envs), and emits five doc
files under ./docs/:

    INDEX.md             — flat, scannable map of everything
    AGENTS_INVENTORY.md  — one section per sub-agent
    TOOLS.md             — tool functions grouped by file
    TOOLSETS.md          — BaseToolset classes + tools they bundle
    CALLBACKS.md         — guardrail/callback functions in app/callbacks/

Idempotent. Re-running over an unchanged tree produces byte-identical output
(modulo the timestamp header line).

Wired into ``app/tools/evolution.py:evolution_verify_sandbox`` so every
self-evolution refreshes the docs before pytest runs. Also runnable manually:

    python scripts/gen_docs.py
"""

from __future__ import annotations

import ast
import datetime
import os
import pathlib
import sys
from typing import Iterable

_ENV_ROOT = os.environ.get("GEN_DOCS_ROOT", "").strip()
ROOT = pathlib.Path(_ENV_ROOT).resolve() if _ENV_ROOT else pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

SUB_AGENTS_DIR = ROOT / "app" / "sub_agents"
TOOLS_DIR = ROOT / "app" / "tools"
TOOLSETS_DIR = ROOT / "app" / "toolsets"
CALLBACKS_DIR = ROOT / "app" / "callbacks"
SKILLS_DIR = ROOT / "skills"


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(ROOT))


def _first_doc_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node) or ""
    return doc.strip().splitlines()[0] if doc.strip() else ""


def _is_private(name: str) -> bool:
    return name.startswith("_")


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------


def scan_agents() -> list[dict]:
    """Each sub_agents/*.py is one agent module.

    Returned dicts: ``{file, name, doc, agent_var, line}`` where
    ``agent_var`` is the module-level assignment target whose value is
    ``Agent(...)``. We extract the ``name=`` kwarg from the Agent call when
    present; otherwise we fall back to the variable name.
    """
    items: list[dict] = []
    if not SUB_AGENTS_DIR.is_dir():
        return items
    for path in sorted(SUB_AGENTS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        module_doc = ast.get_docstring(tree) or ""
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                func = node.value.func
                func_name = (
                    func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute)
                    else ""
                )
                if func_name not in {"Agent", "LlmAgent", "SequentialAgent", "ParallelAgent", "LoopAgent"}:
                    continue
                var = node.targets[0]
                var_name = var.id if isinstance(var, ast.Name) else "?"
                agent_name = var_name
                for kw in node.value.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        agent_name = str(kw.value.value)
                items.append({
                    "file": _rel(path),
                    "line": node.lineno,
                    "var": var_name,
                    "name": agent_name,
                    "kind": func_name,
                    "doc": module_doc.strip().splitlines()[0] if module_doc.strip() else "",
                })
    return items


def scan_tools() -> list[dict]:
    items: list[dict] = []
    if not TOOLS_DIR.is_dir():
        return items
    for path in sorted(TOOLS_DIR.rglob("*.py")):
        if path.name.startswith("_"):
            continue
        rel = path.relative_to(ROOT)
        tree = ast.parse(path.read_text(), filename=str(path))
        module_doc = ast.get_docstring(tree) or ""
        funcs: list[dict] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not _is_private(node.name):
                funcs.append({
                    "name": node.name,
                    "line": node.lineno,
                    "async": isinstance(node, ast.AsyncFunctionDef),
                    "doc": _first_doc_line(node),
                })
        if funcs:
            items.append({
                "file": str(rel),
                "module_doc": module_doc.strip().splitlines()[0] if module_doc.strip() else "",
                "funcs": funcs,
            })
    return items


def scan_toolsets() -> list[dict]:
    items: list[dict] = []
    if not TOOLSETS_DIR.is_dir():
        return items
    for path in sorted(TOOLSETS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                base_names = [
                    b.id if isinstance(b, ast.Name)
                    else b.attr if isinstance(b, ast.Attribute) else ""
                    for b in node.bases
                ]
                if "BaseToolset" not in base_names:
                    continue
                # collect FunctionTool(func=X) refs inside get_tools body
                bundled: list[str] = []
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        func = sub.func
                        if isinstance(func, ast.Name) and func.id == "FunctionTool":
                            for kw in sub.keywords:
                                if kw.arg == "func" and isinstance(kw.value, ast.Name):
                                    bundled.append(kw.value.id)
                items.append({
                    "file": _rel(path),
                    "line": node.lineno,
                    "name": node.name,
                    "doc": _first_doc_line(node),
                    "bundled": sorted(set(bundled)),
                })
    return items


def scan_callbacks() -> list[dict]:
    """ADK 1.x callback conventions:

    - ``before_model`` / ``state_setter`` / ``admin_only_guardrail`` —
      first arg is ``callback_context``.
    - ``before_tool`` / ``after_tool`` — first arg is ``tool``, second
      ``args``, third ``tool_context``.

    We surface either shape; everything else (private helpers, plain
    utilities) is ignored.
    """
    items: list[dict] = []
    if not CALLBACKS_DIR.is_dir():
        return items
    for path in sorted(CALLBACKS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _is_private(node.name):
                continue
            args = [a.arg for a in node.args.args]
            hook = ""
            if args and args[0] == "callback_context":
                hook = "before_model | state"
            elif len(args) >= 3 and args[0] == "tool" and args[1] == "args" and args[2] == "tool_context":
                hook = "before_tool | after_tool"
            else:
                continue
            items.append({
                "file": _rel(path),
                "line": node.lineno,
                "name": node.name,
                "doc": _first_doc_line(node),
                "async": isinstance(node, ast.AsyncFunctionDef),
                "hook": hook,
            })
    return items


def scan_skills() -> list[dict]:
    items: list[dict] = []
    if not SKILLS_DIR.is_dir():
        return items
    for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        text = skill_md.read_text()
        name = skill_md.parent.name
        description = ""
        # crude YAML frontmatter parse — first ``description: "..."`` line
        if text.startswith("---"):
            head = text.split("---", 2)
            if len(head) >= 3:
                for line in head[1].splitlines():
                    if line.strip().startswith("description:"):
                        description = line.split(":", 1)[1].strip().strip('"').strip("'")
                        break
        items.append({"name": name, "file": _rel(skill_md), "description": description})
    return items


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


HEADER = (
    "<!-- AUTO-GENERATED by scripts/gen_docs.py — do not edit by hand. -->\n"
    "<!-- Run `uv run python scripts/gen_docs.py` to refresh. -->\n\n"
)


def render_index(agents, tools, toolsets, callbacks, skills) -> str:
    lines = [HEADER, "# Ori Symbol Index\n"]
    lines.append("Auto-generated map of every sub-agent, tool, toolset, callback, and skill.\n")
    lines.append("Read this **before** adding new code — most things you'd build already exist.\n\n")

    lines.append(f"## Sub-agents ({len(agents)})\n\n")
    for a in agents:
        link = f"`{a['file']}:{a['line']}`"
        lines.append(f"- **{a['name']}** ({a['kind']}) — {link}")
        if a["doc"]:
            lines.append(f"  - {a['doc']}")
        lines.append("")

    total_tools = sum(len(t["funcs"]) for t in tools)
    lines.append(f"## Tools ({total_tools} public functions across {len(tools)} files)\n\n")
    for t in tools:
        lines.append(f"### `{t['file']}`")
        if t["module_doc"]:
            lines.append(f"{t['module_doc']}\n")
        for f in t["funcs"]:
            prefix = "async " if f["async"] else ""
            doc = f" — {f['doc']}" if f["doc"] else ""
            lines.append(f"- `{prefix}{f['name']}` (line {f['line']}){doc}")
        lines.append("")

    lines.append(f"## Toolsets ({len(toolsets)})\n\n")
    for ts in toolsets:
        lines.append(f"- **{ts['name']}** — `{ts['file']}:{ts['line']}`")
        if ts["doc"]:
            lines.append(f"  - {ts['doc']}")
        if ts["bundled"]:
            lines.append(f"  - tools: {', '.join(f'`{b}`' for b in ts['bundled'])}")
        lines.append("")

    lines.append(f"## Callbacks ({len(callbacks)})\n\n")
    for c in callbacks:
        prefix = "async " if c["async"] else ""
        doc = f" — {c['doc']}" if c["doc"] else ""
        lines.append(f"- `{prefix}{c['name']}` ({c['hook']}) — `{c['file']}:{c['line']}`{doc}")
    lines.append("")

    lines.append(f"## Skills ({len(skills)})\n\n")
    for s in skills:
        desc = f" — {s['description']}" if s["description"] else ""
        lines.append(f"- **{s['name']}** — `{s['file']}`{desc}")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_agents(agents) -> str:
    lines = [HEADER, "# Sub-Agents Inventory\n"]
    lines.append("One section per sub-agent. Source of truth for hierarchy, instruction, attached toolsets.\n")
    lines.append("Augment by hand for fields the AST cannot infer (parent_agent, when_to_use, etc).\n\n")
    for a in agents:
        lines.append(f"## {a['name']}")
        lines.append(f"- File: `{a['file']}:{a['line']}` ({a['kind']})")
        lines.append(f"- Module variable: `{a['var']}`")
        if a["doc"]:
            lines.append(f"- Module doc: {a['doc']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_tools(tools) -> str:
    lines = [HEADER, "# Tools\n"]
    lines.append("All public tool functions grouped by file. Private helpers (`_name`) excluded.\n\n")
    for t in tools:
        lines.append(f"## `{t['file']}`")
        if t["module_doc"]:
            lines.append(f"{t['module_doc']}\n")
        for f in t["funcs"]:
            prefix = "async " if f["async"] else ""
            doc = f" — {f['doc']}" if f["doc"] else ""
            lines.append(f"- `{prefix}{f['name']}` (line {f['line']}){doc}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_toolsets(toolsets) -> str:
    lines = [HEADER, "# Toolsets\n"]
    lines.append("`BaseToolset` subclasses. Agents prefer these over raw tool imports.\n\n")
    for ts in toolsets:
        lines.append(f"## {ts['name']}")
        lines.append(f"- File: `{ts['file']}:{ts['line']}`")
        if ts["doc"]:
            lines.append(f"- {ts['doc']}")
        if ts["bundled"]:
            lines.append(f"- Bundled tools: {', '.join(f'`{b}`' for b in ts['bundled'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_callbacks(callbacks) -> str:
    lines = [HEADER, "# Callbacks\n"]
    lines.append("Module-level callbacks. Hook is inferred from the function signature.\n\n")
    for c in callbacks:
        prefix = "async " if c["async"] else ""
        doc = f" — {c['doc']}" if c["doc"] else ""
        lines.append(f"- `{prefix}{c['name']}` ({c['hook']}) — `{c['file']}:{c['line']}`{doc}")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Orphan-proposal warnings
# ---------------------------------------------------------------------------


def warn_orphan_evolutions() -> list[str]:
    """Phase 5 hook — report evolutions/*/ directories without an EVOLUTION.md."""
    evos = ROOT / "evolutions"
    warnings: list[str] = []
    if not evos.is_dir():
        return warnings
    for child in sorted(evos.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not (child / "EVOLUTION.md").is_file():
            warnings.append(f"orphan evolution proposal (no EVOLUTION.md): {_rel(child)}")
    return warnings


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


def main(argv: Iterable[str] = ()) -> int:
    DOCS.mkdir(exist_ok=True)
    agents = scan_agents()
    tools = scan_tools()
    toolsets = scan_toolsets()
    callbacks = scan_callbacks()
    skills = scan_skills()

    outputs = {
        DOCS / "INDEX.md": render_index(agents, tools, toolsets, callbacks, skills),
        DOCS / "AGENTS_INVENTORY.md": render_agents(agents),
        DOCS / "TOOLS.md": render_tools(tools),
        DOCS / "TOOLSETS.md": render_toolsets(toolsets),
        DOCS / "CALLBACKS.md": render_callbacks(callbacks),
    }

    changed = 0
    for path, content in outputs.items():
        existing = path.read_text() if path.is_file() else ""
        if existing != content:
            path.write_text(content)
            changed += 1

    print(f"gen_docs: agents={len(agents)} tools={sum(len(t['funcs']) for t in tools)} "
          f"toolsets={len(toolsets)} callbacks={len(callbacks)} skills={len(skills)} "
          f"files_changed={changed}")

    for w in warn_orphan_evolutions():
        print(f"warning: {w}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
