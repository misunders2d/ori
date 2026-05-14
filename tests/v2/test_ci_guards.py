"""Tests for scripts/check_phase_scope.py.

Pins docs/PHASE_1_PLAN.md §6 behaviour:

- Phase-1 allowlist accepts files under `app/v2/`, `tests/v2/`,
  the named CI-guard scripts, the design docs, the
  `.v2-current-phase` tracking file, and the doc-read marker.
- Phases 1 through 7 reject any touch to v1 scheduler paths
  (``app/contracts/``, ``app/tasks.py``,
  ``app/scheduler_instance.py``, ``data/contracts/``).
- Cross-phase diffs are rejected when the current phase's
  allowlist doesn't cover them.
- The guard goes inert when `.v2-current-phase` is missing or
  empty, so the script can ship before phase 1 starts on this
  branch and silently no-op on other branches.

Tests evaluate the pure-Python ``check_phase_scope`` module
directly — no shell subprocess invocations. The shell wrapper at
`.githooks/v2_phase_guard.sh` is one-line; if it works for the
script it works for the wrapper.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest


# ---------------------------------------------------------------------------
# Module loader
# ---------------------------------------------------------------------------


def _load_phase_scope_module():
    """Import ``scripts/check_phase_scope.py`` as a module under the
    name ``check_phase_scope``. The repo's `scripts/` is NOT on
    sys.path by default; spec-load lets the tests run without
    polluting the package layout."""
    here = pathlib.Path(__file__).resolve()
    repo_root = here.parents[2]
    script_path = repo_root / "scripts" / "check_phase_scope.py"
    spec = importlib.util.spec_from_file_location(
        "check_phase_scope", str(script_path)
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    return _load_phase_scope_module()


# ---------------------------------------------------------------------------
# Path-match helpers
# ---------------------------------------------------------------------------


def test_matches_directory_prefix(guard):
    """Patterns ending in ``/`` match any path starting with that
    prefix (directory-style match)."""
    assert guard._matches("app/v2/enums.py", "app/v2/") is True
    assert guard._matches("app/v2/models/schedule.py", "app/v2/") is True
    assert guard._matches("app/v2", "app/v2/") is True  # bare dir name OK


def test_matches_exact_file(guard):
    """Patterns without a trailing slash require exact equality."""
    assert guard._matches(
        "scripts/check_phase_scope.py",
        "scripts/check_phase_scope.py",
    ) is True
    assert guard._matches(
        "scripts/install_hooks.py",
        "scripts/check_phase_scope.py",
    ) is False


def test_matches_unrelated_path(guard):
    assert guard._matches("app/tools/slack.py", "app/v2/") is False
    assert guard._matches("docs/CONTRACTS.md", "docs/PHASE_1_PLAN.md") is False


# ---------------------------------------------------------------------------
# Phase-1 allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "app/v2/enums.py",
        "app/v2/models/schedule.py",
        "app/v2/migrations/v001_initial.py",
        "tests/v2/test_models_schedule.py",
        "tests/v2/conftest.py",
        "scripts/check_phase_scope.py",
        "scripts/install_hooks.py",
        ".githooks/v2_phase_guard.sh",
        ".githooks/pre-commit",
        ".github/workflows/v2_phase_guard.yml",
        ".v2-current-phase",
        "docs/PHASE_1_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        ".docs_read_marker",
    ],
)
def test_phase_1_allowlist_accepts(guard, path):
    assert guard.is_allowed(path, 1) is True


@pytest.mark.parametrize(
    "path",
    [
        "app/contracts/schema.py",
        "app/tasks.py",
        "app/scheduler_instance.py",
        "app/agent.py",
        "app/sub_agents/coordinator_agent.py",
        "docs/CONTRACTS.md",
        "pyproject.toml",
        "uv.lock",
        "data/contracts/foo/v1__abc.json",
    ],
)
def test_phase_1_allowlist_rejects(guard, path):
    assert guard.is_allowed(path, 1) is False


# ---------------------------------------------------------------------------
# Forbidden v1 paths (phases 1-7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "app/contracts/schema.py",
        "app/contracts/executor.py",
        "app/contracts/worker.py",
        "app/contracts/emit.py",
        "app/contracts/admin_alert.py",
        "app/tasks.py",
        "app/scheduler_instance.py",
        "data/contracts/linux_mastery/v1__abc.json",
    ],
)
@pytest.mark.parametrize("phase", [1, 2, 3, 4, 5, 6, 7])
def test_v1_paths_forbidden_phases_1_to_7(guard, path, phase):
    """The forbidden set must reject for every phase in the
    protected window, even phases that don't yet exist in the
    PHASE_ALLOWLIST dict."""
    assert guard.is_forbidden(path, phase) is True


@pytest.mark.parametrize("phase", [8, 9, 10, 16])
def test_v1_paths_permissible_from_phase_8(guard, phase):
    """Phase 8 (first end-to-end OneOffReminder) is the boundary
    where v1 edits become permissible — compatibility worker lands
    there; v1 deprecation kicks in from there forward."""
    assert guard.is_forbidden("app/contracts/schema.py", phase) is False
    assert guard.is_forbidden("app/tasks.py", phase) is False


# ---------------------------------------------------------------------------
# evaluate() integration — combines allowlist + forbidden check
# ---------------------------------------------------------------------------


def test_evaluate_clean_diff_returns_no_violations(guard):
    files = [
        "app/v2/enums.py",
        "tests/v2/test_models_schedule.py",
        "docs/PHASE_1_PLAN.md",
    ]
    assert guard.evaluate(files, phase=1) == []


def test_evaluate_v1_path_flagged_as_forbidden(guard):
    files = ["app/v2/enums.py", "app/contracts/schema.py"]
    violations = guard.evaluate(files, phase=1)
    assert len(violations) == 1
    assert "forbidden" in violations[0]
    assert "app/contracts/schema.py" in violations[0]


def test_evaluate_off_allowlist_flagged_as_out_of_scope(guard):
    files = ["app/v2/enums.py", "app/sub_agents/coordinator_agent.py"]
    violations = guard.evaluate(files, phase=1)
    assert len(violations) == 1
    assert "allowlist" in violations[0]
    assert "coordinator_agent.py" in violations[0]


def test_evaluate_multiple_violations_reported(guard):
    files = [
        "app/v2/enums.py",                    # OK
        "app/contracts/schema.py",            # forbidden
        "app/sub_agents/coordinator_agent.py",  # off allowlist
        "pyproject.toml",                     # off allowlist
    ]
    violations = guard.evaluate(files, phase=1)
    assert len(violations) == 3


def test_evaluate_forbidden_takes_precedence_over_allowlist(guard):
    """If a path is both 'not in allowlist' AND 'forbidden', the
    error message should call it forbidden — the user needs to know
    they're touching a v1 path that's never coming back into scope
    until phase 8, not just 'add it to allowlist'."""
    violations = guard.evaluate(["app/contracts/schema.py"], phase=1)
    assert len(violations) == 1
    assert "forbidden" in violations[0]


# ---------------------------------------------------------------------------
# current_phase() reads the tracking file
# ---------------------------------------------------------------------------


def test_current_phase_reads_file(guard, tmp_path, monkeypatch):
    target = tmp_path / ".v2-current-phase"
    target.write_text("1\n")
    monkeypatch.chdir(tmp_path)
    assert guard.current_phase() == 1


def test_current_phase_returns_none_when_missing(guard, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert guard.current_phase() is None


def test_current_phase_returns_none_when_empty(guard, tmp_path, monkeypatch):
    target = tmp_path / ".v2-current-phase"
    target.write_text("")
    monkeypatch.chdir(tmp_path)
    assert guard.current_phase() is None


def test_current_phase_returns_none_when_garbled(guard, tmp_path, monkeypatch):
    """If the tracking file is unparseable, the guard is inert (returns
    None) — we'd rather miss a check than block every commit."""
    target = tmp_path / ".v2-current-phase"
    target.write_text("phase one\n")
    monkeypatch.chdir(tmp_path)
    assert guard.current_phase() is None


# ---------------------------------------------------------------------------
# main() entry — exit-code contract
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_phase_missing(guard, tmp_path, monkeypatch):
    """Without a `.v2-current-phase` file, the guard returns 0 for
    any input. This lets the script ship to branches that aren't
    running a v2 phase without breaking their commits."""
    monkeypatch.chdir(tmp_path)
    # phase override = None means the script reads the file (absent),
    # then returns 0 without inspecting the diff.
    assert guard.main(["--staged"]) == 0


def test_main_returns_zero_on_clean_diff(guard, monkeypatch):
    """Inject a phase override so the script doesn't need to read
    the tracking file; mock the staged-files lookup to return a
    clean list."""
    monkeypatch.setattr(
        guard,
        "get_staged_files",
        lambda: ["app/v2/enums.py", "tests/v2/test_models_schedule.py"],
    )
    assert guard.main(["--staged", "--phase", "1"]) == 0


def test_main_returns_one_on_violation(guard, monkeypatch, capsys):
    monkeypatch.setattr(
        guard,
        "get_staged_files",
        lambda: ["app/v2/enums.py", "app/contracts/schema.py"],
    )
    rc = guard.main(["--staged", "--phase", "1"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "PHASE 1 SCOPE VIOLATION" in captured.err
    assert "app/contracts/schema.py" in captured.err
    assert "PHASE_OVERRIDE" in captured.err


def test_main_diff_mode_uses_get_diff_files(guard, monkeypatch):
    """--diff mode calls get_diff_files instead of get_staged_files.
    Confirms the CI path is exercised distinct from the pre-commit
    path."""
    calls = {}

    def fake_diff(ref):
        calls["ref"] = ref
        return ["app/v2/enums.py"]

    monkeypatch.setattr(guard, "get_diff_files", fake_diff)
    rc = guard.main(["--diff", "origin/master", "--phase", "1"])
    assert rc == 0
    assert calls["ref"] == "origin/master"


def test_main_diff_mode_flags_forbidden(guard, monkeypatch, capsys):
    monkeypatch.setattr(
        guard, "get_diff_files",
        lambda ref: ["app/contracts/schema.py", "app/v2/enums.py"],
    )
    rc = guard.main(["--diff", "origin/master", "--phase", "1"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "forbidden" in captured.err


def test_main_returns_zero_when_diff_empty(guard, monkeypatch):
    """An empty diff (no changed files) is always clean."""
    monkeypatch.setattr(guard, "get_staged_files", lambda: [])
    assert guard.main(["--staged", "--phase", "1"]) == 0
