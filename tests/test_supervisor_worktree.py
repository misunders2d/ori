"""Supervisor worktree/branch awareness — regression test for the
May 2026 incident class of bugs.

`deploy/ori-supervisor.py` historically hardcoded ``master`` in
``apply_evolution()`` and ``apply_rollback()``. On a worktree or any
deploy box checked out on a feature/release branch, that fetched the
wrong branch and ``git reset --hard FETCH_HEAD`` could clobber local
commits. CLAUDE.md and docs/RUNBOOK.md §3 warn explicitly against this.

These tests exercise the worktree-aware helpers (`_current_branch`,
`_upstream_branch`) plus the two evolution paths against a real temp
git repo, by monkeypatching ``PROJECT_ROOT`` to point at the temp repo.

We don't test the network path (no GITHUB_TOKEN/REPO) because that would
require real GitHub credentials — we exercise the local-evolution fallback
and the refusal paths instead, which is where the worktree-awareness
logic lives.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess

import pytest


_SUPERVISOR_PATH = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "ori-supervisor.py"


@pytest.fixture
def supervisor_module(tmp_path, monkeypatch):
    """Import ``deploy/ori-supervisor.py`` with PROJECT_ROOT redirected
    to a fresh temp git repo. Each test gets a private repo so branch
    state can be mutated freely.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README").write_text("ok\n")
    subprocess.run(["git", "add", "README"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    # The module reads vault + tries to set up logging — keep that happy
    # by pointing it at our temp dir before import.
    monkeypatch.chdir(repo)
    monkeypatch.setenv("HOME", str(tmp_path))

    spec = importlib.util.spec_from_file_location("ori_supervisor_test", _SUPERVISOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    # The module sets PROJECT_ROOT via __file__ during import. Override
    # it post-import to our tmp repo, plus override the logger so we
    # don't write to data/agent.log.
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "PROJECT_ROOT", str(repo))
    return mod, repo


def _create_branch(repo: pathlib.Path, branch: str):
    subprocess.run(["git", "checkout", "-qb", branch], cwd=repo, check=True)


def _detach_head(repo: pathlib.Path):
    # Detach onto the current commit.
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=repo, check=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_current_branch_on_named_branch(supervisor_module):
    mod, repo = supervisor_module
    _create_branch(repo, "evo/amazon_manager")
    assert mod._current_branch() == "evo/amazon_manager"


def test_current_branch_none_on_detached_head(supervisor_module):
    mod, repo = supervisor_module
    _detach_head(repo)
    assert mod._current_branch() is None


def test_upstream_branch_none_when_no_upstream(supervisor_module):
    mod, repo = supervisor_module
    _create_branch(repo, "evo/amazon_manager")
    # No remote configured yet — upstream lookup must return None,
    # not raise.
    assert mod._upstream_branch("evo/amazon_manager") is None


# ---------------------------------------------------------------------------
# apply_evolution
# ---------------------------------------------------------------------------


def test_apply_evolution_refuses_on_detached_head(supervisor_module, caplog):
    mod, repo = supervisor_module
    _detach_head(repo)

    with caplog.at_level("ERROR"):
        mod.apply_evolution()

    assert any(
        "Refusing apply_evolution" in r.message and "detached" in r.message
        for r in caplog.records
    ), "supervisor must refuse on detached HEAD with a clear error"


def test_apply_evolution_local_path_uses_current_branch_not_master(
    supervisor_module, monkeypatch, tmp_path
):
    """Without GITHUB_TOKEN/REPO, the supervisor takes the local-evolution
    fallback. It used to run ``git checkout master -- .`` — that's the
    bug. It must instead refresh from the *current* branch.
    """
    mod, repo = supervisor_module

    _create_branch(repo, "evo/amazon_manager")
    # Make a second commit so HEAD has something distinguishable from
    # the initial commit.
    (repo / "f.txt").write_text("v1\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "v1"], cwd=repo, check=True)

    # Ensure vault.get returns None for GITHUB_*.
    monkeypatch.setattr(mod, "get", lambda key: None)
    # Also stub deps_changed to avoid the uv sync side-trip during the test.
    monkeypatch.setattr(mod, "deps_changed", lambda: False)

    captured: list[list[str]] = []

    real_run = subprocess.run

    def spy_run(cmd, *args, **kwargs):
        captured.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", spy_run)

    mod.apply_evolution()

    # The git checkout call must reference our branch (or its upstream),
    # never the hardcoded literal "master".
    checkout_calls = [
        c for c in captured if len(c) >= 2 and c[0] == "git" and c[1] == "checkout"
    ]
    assert checkout_calls, "expected at least one `git checkout` call"
    for c in checkout_calls:
        assert "master" not in c, (
            f"supervisor still references master in apply_evolution: {c}"
        )
        # Either bare branch name or 'origin/<branch>' is acceptable.
        assert any(
            tok == "evo/amazon_manager" or tok.endswith("/evo/amazon_manager")
            for tok in c
        ), f"checkout must target the current branch, got: {c}"


# ---------------------------------------------------------------------------
# apply_rollback
# ---------------------------------------------------------------------------


def test_apply_rollback_refuses_on_detached_head(supervisor_module, caplog):
    mod, repo = supervisor_module
    _detach_head(repo)

    with caplog.at_level("ERROR"):
        mod.apply_rollback()

    assert any(
        "Refusing apply_rollback" in r.message and "detached" in r.message
        for r in caplog.records
    ), "rollback on detached HEAD would orphan the previous tip — must refuse"


def test_apply_rollback_runs_on_named_branch(supervisor_module, monkeypatch):
    mod, repo = supervisor_module
    _create_branch(repo, "evo/amazon_manager")
    # Two commits so HEAD~1 resolves.
    (repo / "f.txt").write_text("v1\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "v1"], cwd=repo, check=True)
    (repo / "f.txt").write_text("v2\n")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "v2"], cwd=repo, check=True)

    monkeypatch.setattr(mod, "deps_changed", lambda: False)
    mod.apply_rollback()

    head = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=repo, capture_output=True, text=True
    )
    assert head.stdout.strip() == "v1", "rollback must move HEAD back by one commit"
