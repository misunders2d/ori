"""Phase 8.2 — pytest assertion that auto-gen docs are in sync with code.

Catches commits where someone bypassed the pre-commit hook (e.g. via
`git commit --no-verify`) and shipped a stale `docs/INDEX.md`. If this
test fails, the fix is one line:

    uv run python scripts/gen_docs.py

It will overwrite the five auto-gen docs with content matching the
current code. Commit the change.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib


_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_AUTO_DOCS = (
    "docs/INDEX.md",
    "docs/AGENTS_INVENTORY.md",
    "docs/TOOLS.md",
    "docs/TOOLSETS.md",
    "docs/CALLBACKS.md",
)


def _load_gen_docs():
    spec = importlib.util.spec_from_file_location(
        "gen_docs_freshness_check",
        str(_PROJECT_ROOT / "scripts" / "gen_docs.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_auto_generated_docs_are_in_sync_with_code(tmp_path, monkeypatch):
    """Re-render auto-gen docs into tmp_path and compare to what's on disk.

    Any difference means the live docs are stale vs. the code — a
    `uv run python scripts/gen_docs.py` would fix it. Failure surfaces
    the exact diff so the committer can see what they missed.
    """
    # Point gen_docs at a tmp DOCS dir so it doesn't overwrite live files
    # while we run the test. ROOT stays the real repo so scans see all
    # the actual agents / tools / toolsets / callbacks / skills.
    gen_docs = _load_gen_docs()
    monkeypatch.setattr(gen_docs, "DOCS", tmp_path)
    gen_docs.DOCS.mkdir(exist_ok=True)

    rc = gen_docs.main([])
    assert rc == 0, "gen_docs.py exited non-zero"

    drifted: list[tuple[str, str, str]] = []
    for rel in _AUTO_DOCS:
        live = _PROJECT_ROOT / rel
        regen = tmp_path / pathlib.Path(rel).name
        live_text = live.read_text() if live.is_file() else ""
        regen_text = regen.read_text() if regen.is_file() else ""
        if live_text != regen_text:
            drifted.append((rel, live_text, regen_text))

    if not drifted:
        return

    lines = [
        "Auto-generated docs are stale vs current code:",
        "",
    ]
    for rel, live, regen in drifted:
        lines.append(f"  {rel}  ({len(live)} bytes live → {len(regen)} bytes regenerated)")
    lines.append("")
    lines.append(
        "Fix:  uv run python scripts/gen_docs.py    "
        "(commit + re-run this test)."
    )
    raise AssertionError("\n".join(lines))
