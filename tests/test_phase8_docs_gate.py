"""Phase 8 — doc-read gate enforcement.

Two enforcement layers, two test groups:

1. Internal `DeveloperAgent`: `evolution_stage_change` refuses to run
   until `evolution_read_file` has been called for `docs/AI_EDITS.md`
   AND `docs/INDEX.md` in the same session.

2. External AI (Claude Code, etc.): `scripts/check_docs_read.py`
   writes `.docs_read_marker`; `.githooks/pre-commit` reads it.
"""

from __future__ import annotations

import os
import time

import pytest


# ---------------------------------------------------------------------------
# Internal gate (evolution_stage_change requires docs_read flags)
# ---------------------------------------------------------------------------


class _State(dict):
    """Just a dict that also exposes `.to_dict()` like ADK state does."""

    def to_dict(self):
        return dict(self)


class _Ctx:
    """Minimal tool_context stand-in: only `.state` is needed by the gate."""

    def __init__(self, user_id: str = "alice@example.com"):
        self.state = _State({"user_id": user_id})


@pytest.fixture
def in_repo_dir(monkeypatch, tmp_path):
    """Run with cwd=tmp_path so evolution_stage_change writes there
    instead of polluting the live ./data/sandbox."""
    monkeypatch.chdir(tmp_path)


def test_stage_change_refuses_without_docs_read(in_repo_dir):
    """First call to stage_change must fail with needs_docs_read."""
    from app.tools.evolution import _DOC_READ_REQUIRED, evolution_stage_change

    ctx = _Ctx()
    result = evolution_stage_change(
        "app/scratch.py",
        "def hello() -> str:\n    return 'world'\n",
        ctx,
    )
    assert result["status"] == "needs_docs_read"
    assert sorted(result["missing_docs"]) == sorted(_DOC_READ_REQUIRED)
    # The refusal message must list the exact tool calls needed.
    assert "evolution_read_file" in result["message"]
    assert "docs/AI_EDITS.md" in result["message"]
    assert "docs/INDEX.md" in result["message"]


def test_stage_change_refuses_with_only_one_doc_read(in_repo_dir):
    """Reading one of the two required docs is not enough."""
    from app.tools.evolution import _DOC_READ_REQUIRED, _mark_doc_read, evolution_stage_change

    ctx = _Ctx()
    _mark_doc_read(ctx, "docs/AI_EDITS.md")

    result = evolution_stage_change("app/scratch.py", "x = 1\n", ctx)
    assert result["status"] == "needs_docs_read"
    assert result["missing_docs"] == ["docs/INDEX.md"]


def test_stage_change_proceeds_after_both_docs_read(in_repo_dir):
    """Both docs read in session → stage_change proceeds normally."""
    from app.tools.evolution import _mark_doc_read, evolution_stage_change

    ctx = _Ctx()
    _mark_doc_read(ctx, "docs/AI_EDITS.md")
    _mark_doc_read(ctx, "docs/INDEX.md")

    result = evolution_stage_change(
        "app/scratch.py",
        "x = 1\n",
        ctx,
    )
    assert result["status"] == "success"
    # Sandbox file was actually written.
    assert os.path.isfile(os.path.abspath("./data/sandbox/app/scratch.py"))


def test_read_file_flips_flag_for_required_docs(monkeypatch, tmp_path):
    """evolution_read_file sets the docs_read flag, but only for required docs."""
    from app.tools.evolution import evolution_read_file

    # Make PROJECT_ROOT point at our tmp_path so reads stay sandboxed.
    monkeypatch.setattr("app.tools.evolution.PROJECT_ROOT", str(tmp_path))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AI_EDITS.md").write_text("# rules")
    (tmp_path / "docs" / "INDEX.md").write_text("# index")
    (tmp_path / "docs" / "OTHER.md").write_text("# other")

    ctx = _Ctx()

    evolution_read_file("docs/AI_EDITS.md", ctx)
    assert ctx.state.get("docs_read", {}).get("docs/AI_EDITS.md") is True

    evolution_read_file("docs/OTHER.md", ctx)
    # Non-required doc does NOT flip an unrelated flag.
    assert "docs/OTHER.md" not in ctx.state.get("docs_read", {})

    evolution_read_file("docs/INDEX.md", ctx)
    assert ctx.state.get("docs_read", {}).get("docs/INDEX.md") is True


def test_normalize_doc_path_handles_prefix_variants():
    from app.tools.evolution import _normalize_doc_path

    assert _normalize_doc_path("docs/AI_EDITS.md") == "docs/AI_EDITS.md"
    assert _normalize_doc_path("./docs/AI_EDITS.md") == "docs/AI_EDITS.md"
    assert _normalize_doc_path("docs\\AI_EDITS.md") == "docs/AI_EDITS.md"
    assert _normalize_doc_path("") == ""
    assert _normalize_doc_path(None) == ""  # type: ignore[arg-type]


def test_audit_logs_stage_gate_refusal(tmp_path, monkeypatch, in_repo_dir):
    """A refused stage_change emits a `stage/fail` audit event tagged with the gate."""
    import json

    from app.tools import evolution

    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(evolution, "_EVOLUTION_AUDIT_PATH", str(audit))

    ctx = _Ctx()
    res = evolution.evolution_stage_change("app/x.py", "x=1\n", ctx)
    assert res["status"] == "needs_docs_read"

    # Audit log has the gate-refusal event.
    lines = audit.read_text().splitlines()
    assert lines, "audit log should have at least one line"
    event = json.loads(lines[-1])
    assert event["phase"] == "stage"
    assert event["status"] == "fail"
    assert event["error"] == "docs_read_gate"
    assert "docs/AI_EDITS.md" in event["missing_docs"]


# ---------------------------------------------------------------------------
# External gate (check_docs_read.py + pre-commit hook)
# ---------------------------------------------------------------------------


def test_check_docs_read_writes_marker(tmp_path, monkeypatch):
    """`check_docs_read.py --non-interactive` writes the marker."""
    import importlib.util
    import sys

    # Set up a minimal repo: required docs present + a scripts/ dir.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "AI_EDITS.md").write_text("rules")
    (tmp_path / "docs" / "INDEX.md").write_text("index")

    # Import the script under a redirected REPO_ROOT so it works against tmp_path.
    spec = importlib.util.spec_from_file_location(
        "check_docs_read_test",
        os.path.abspath("scripts/check_docs_read.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "MARKER_PATH", tmp_path / ".docs_read_marker")

    rc = mod.main(["--non-interactive"])
    assert rc == 0
    marker = tmp_path / ".docs_read_marker"
    assert marker.is_file()
    text = marker.read_text()
    assert "docs/AI_EDITS.md" in text
    assert "docs/INDEX.md" in text


def test_check_docs_read_missing_doc_aborts(tmp_path, monkeypatch):
    """Missing docs/AI_EDITS.md → exit code 2, no marker written."""
    import importlib.util

    (tmp_path / "docs").mkdir()
    # AI_EDITS deliberately missing.
    (tmp_path / "docs" / "INDEX.md").write_text("index")

    spec = importlib.util.spec_from_file_location(
        "check_docs_read_missing_test",
        os.path.abspath("scripts/check_docs_read.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "MARKER_PATH", tmp_path / ".docs_read_marker")

    rc = mod.main(["--non-interactive"])
    assert rc == 2
    assert not (tmp_path / ".docs_read_marker").exists()


def test_pre_commit_hook_blocks_when_marker_missing(tmp_path):
    """Run the pre-commit hook in a tmp dir without the marker → exits non-zero."""
    import shutil
    import subprocess

    hook_src = os.path.abspath(".githooks/pre-commit")
    assert os.path.isfile(hook_src), "expected .githooks/pre-commit to exist"
    hook = tmp_path / "pre-commit"
    shutil.copy(hook_src, hook)
    hook.chmod(0o755)

    result = subprocess.run([str(hook)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0
    assert "doc-read marker" in result.stderr.lower()


def test_pre_commit_hook_passes_with_fresh_marker(tmp_path):
    """Fresh marker (just created) → hook exits 0."""
    import shutil
    import subprocess

    hook_src = os.path.abspath(".githooks/pre-commit")
    hook = tmp_path / "pre-commit"
    shutil.copy(hook_src, hook)
    hook.chmod(0o755)

    (tmp_path / ".docs_read_marker").write_text("test marker")
    result = subprocess.run([str(hook)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_pre_commit_hook_blocks_with_stale_marker(tmp_path):
    """Marker older than 2 hours → hook exits non-zero (stale)."""
    import shutil
    import subprocess

    hook_src = os.path.abspath(".githooks/pre-commit")
    hook = tmp_path / "pre-commit"
    shutil.copy(hook_src, hook)
    hook.chmod(0o755)

    marker = tmp_path / ".docs_read_marker"
    marker.write_text("stale")
    stale_time = time.time() - (3 * 3600)  # 3 h ago
    os.utime(marker, (stale_time, stale_time))

    result = subprocess.run([str(hook)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode != 0
    assert "stale" in result.stderr.lower()


def _make_real_git_repo(tmp_path):
    """Spin up a tiny git repo so the design-doc check (which uses
    ``git diff --cached``) has something real to inspect."""
    import shutil
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    hook_src = os.path.abspath(".githooks/pre-commit")
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_dst = hooks_dir / "pre-commit"
    shutil.copy(hook_src, hook_dst)
    hook_dst.chmod(0o755)

    # Fresh marker so the marker gate passes — we're testing the new
    # docs-check, not the marker check.
    (repo / ".docs_read_marker").write_text("ok")

    return repo, hook_dst


def test_pre_commit_blocks_when_behaviour_code_staged_without_docs(tmp_path):
    """Staging ``app/callbacks/foo.py`` (a behaviour-relevant file) WITHOUT
    any ``docs/*.md`` change must trip the docs-check and refuse the commit."""
    import subprocess

    repo, hook = _make_real_git_repo(tmp_path)

    cb = repo / "app" / "callbacks"
    cb.mkdir(parents=True)
    (cb / "foo.py").write_text("# fake callback\n")
    subprocess.run(["git", "add", "app/callbacks/foo.py"], cwd=repo, check=True)

    result = subprocess.run([str(hook)], cwd=repo, capture_output=True, text=True)
    assert result.returncode != 0, (
        f"hook should have blocked but exited 0\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "behaviour-relevant code" in result.stderr.lower() or (
        "docs" in result.stderr.lower() and "blocked" in result.stderr.lower()
    )


def test_pre_commit_allows_behaviour_code_with_docs_in_same_commit(tmp_path):
    """Same code change, but a ``docs/*.md`` edit also staged → hook
    passes. This is the green path for design-doc compliance."""
    import subprocess

    repo, hook = _make_real_git_repo(tmp_path)

    cb = repo / "app" / "callbacks"
    cb.mkdir(parents=True)
    (cb / "foo.py").write_text("# fake callback\n")
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "CALLBACKS.md").write_text("# callbacks doc\nfake foo registered.\n")

    subprocess.run(["git", "add", "app/callbacks/foo.py", "docs/CALLBACKS.md"], cwd=repo, check=True)

    result = subprocess.run([str(hook)], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"hook should have passed but exited {result.returncode}\n"
        f"stderr: {result.stderr}"
    )


def test_pre_commit_bypass_env_var_skips_docs_check(tmp_path):
    """``ORI_SKIP_DOC_CHECK=1`` lets genuinely doc-irrelevant commits
    (typo, lint, dead-code removal) through without forcing a doc
    edit. Escape hatch for the rare legitimate case."""
    import subprocess

    repo, hook = _make_real_git_repo(tmp_path)

    cb = repo / "app" / "callbacks"
    cb.mkdir(parents=True)
    (cb / "typofix.py").write_text("# typo fix only\n")
    subprocess.run(["git", "add", "app/callbacks/typofix.py"], cwd=repo, check=True)

    env = os.environ.copy()
    env["ORI_SKIP_DOC_CHECK"] = "1"
    result = subprocess.run(
        [str(hook)], cwd=repo, capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, (
        f"bypass env var should have let commit through, got rc={result.returncode}\n"
        f"stderr: {result.stderr}"
    )


def test_pre_commit_allows_pure_doc_change(tmp_path):
    """A commit that only touches ``docs/*.md`` (no code) must not be
    blocked by the docs-check — the check exists to FORCE docs alongside
    code, not to require both directions."""
    import subprocess

    repo, hook = _make_real_git_repo(tmp_path)

    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "RUNBOOK.md").write_text("# runbook update\nclarified deploy.\n")
    subprocess.run(["git", "add", "docs/RUNBOOK.md"], cwd=repo, check=True)

    result = subprocess.run([str(hook)], cwd=repo, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
