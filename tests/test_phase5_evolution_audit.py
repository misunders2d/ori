"""Phase 5 tests: evolution audit log + integration cycle.

Pure-Python unit tests against the audit helper, plus an integration
test that runs a no-op stage / verify (syntax) / discard cycle against
a tmp sandbox and verifies the audit log records each event with the
right status.

The full commit path is NOT exercised here — it would push a real git
commit. The pytest verify is also skipped — instead we use the `syntax`
check which is hermetic.
"""

import json
import os

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeState:
    def __init__(self, data: dict):
        self._d = dict(data)

    def to_dict(self):
        return dict(self._d)

    def __setitem__(self, k, v):
        self._d[k] = v

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)


class _FakeSession:
    def __init__(self):
        self.session_id = "evo-audit-test"


class _FakeToolContext:
    def __init__(self, user_id: str = "admin@example.com", docs_read: bool = True):
        # Phase 8 gate: `evolution_stage_change` refuses without docs_read.
        # Default this on so we test the post-stage behaviour. The gate
        # itself is exercised separately in tests/test_phase8_docs_gate.py.
        state = {"user_id": user_id}
        if docs_read:
            state["docs_read"] = {
                "docs/AI_EDITS.md": True,
                "docs/INDEX.md": True,
            }
        self.state = _FakeState(state)
        self.session = _FakeSession()


@pytest.fixture
def isolated_audit(tmp_path, monkeypatch):
    """Route the evolution audit log to a tmp path."""
    from app.tools import evolution

    audit = tmp_path / "evolution_audit.jsonl"
    monkeypatch.setattr(evolution, "_EVOLUTION_AUDIT_PATH", str(audit))
    return audit


def _read_lines(path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Audit helper unit tests
# ---------------------------------------------------------------------------


def test_audit_appends_one_event(isolated_audit):
    from app.tools.evolution import _evolution_audit

    _evolution_audit("stage", "u@x.com", "ok", files=["app/foo.py"])

    events = _read_lines(isolated_audit)
    assert len(events) == 1
    e = events[0]
    assert e["phase"] == "stage"
    assert e["actor"] == "u@x.com"
    assert e["status"] == "ok"
    assert e["files"] == ["app/foo.py"]
    assert "ts" in e


def test_audit_swallows_write_failure(monkeypatch):
    """Audit-log write errors must not raise — evolution flow continues."""
    from app.tools import evolution

    monkeypatch.setattr(
        evolution,
        "_EVOLUTION_AUDIT_PATH",
        "/proc/1/foreign/cannot-write/here/audit.jsonl",
    )

    # Should not raise — exception is logged and swallowed.
    evolution._evolution_audit("stage", "u", "fail", error="anything")


def test_audit_actor_from_tool_context_state():
    from app.tools.evolution import _audit_actor

    assert _audit_actor(_FakeToolContext("alice@x.com")) == "alice@x.com"
    assert _audit_actor(None) == ""


# ---------------------------------------------------------------------------
# Integration: stage → verify(syntax=ok) → discard
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_sandbox(tmp_path, monkeypatch):
    """Redirect the sandbox dir + cwd to tmp_path so the test doesn't
    touch the real ./data/sandbox.
    """
    monkeypatch.chdir(tmp_path)


def test_full_cycle_emits_stage_verify_discard_events(isolated_sandbox, isolated_audit):
    from app.tools.evolution import (
        evolution_discard_sandbox,
        evolution_stage_change,
        evolution_verify_sandbox,
    )

    ctx = _FakeToolContext("e2e@example.com")

    # 1. Stage a single trivial Python file.
    res = evolution_stage_change(
        "app/scratch_e2e.py",
        "def hello() -> str:\n    return 'world'\n",
        ctx,
    )
    assert res["status"] == "success"

    # 2. Run a syntax check — hermetic, no git, no pytest.
    res = evolution_verify_sandbox("syntax", ctx, target="app/scratch_e2e.py")
    assert res["status"] == "success"

    # 3. Abandon the cycle (we don't commit in tests).
    res = evolution_discard_sandbox(ctx)
    assert res["status"] == "success"

    events = _read_lines(isolated_audit)
    phases = [e["phase"] for e in events]
    assert "stage" in phases
    assert "verify" in phases
    assert "discard" in phases

    # Every event must have the e2e actor we set.
    for e in events:
        assert e["actor"] == "e2e@example.com"


def test_verify_failure_emits_fail_audit(isolated_sandbox, isolated_audit):
    from app.tools.evolution import (
        evolution_discard_sandbox,
        evolution_stage_change,
        evolution_verify_sandbox,
    )

    ctx = _FakeToolContext()
    evolution_stage_change(
        "app/broken_e2e.py",
        "def broken(:\n    return 1\n",  # intentional syntax error
        ctx,
    )
    res = evolution_verify_sandbox("syntax", ctx, target="app/broken_e2e.py")
    assert res["status"] == "error"

    evolution_discard_sandbox(ctx)

    events = _read_lines(isolated_audit)
    verify_events = [e for e in events if e["phase"] == "verify"]
    assert len(verify_events) >= 1
    assert any(e["status"] == "fail" for e in verify_events)


def test_discard_on_empty_sandbox_records_noop(isolated_sandbox, isolated_audit):
    from app.tools.evolution import evolution_discard_sandbox

    ctx = _FakeToolContext()
    res = evolution_discard_sandbox(ctx)
    assert res["status"] == "noop"

    events = _read_lines(isolated_audit)
    assert events[-1]["phase"] == "discard"
    assert events[-1]["status"] == "noop"


# ---------------------------------------------------------------------------
# gen_docs orphan warning — Phase 5.2
# ---------------------------------------------------------------------------


def test_gen_docs_warns_about_orphan_evolutions(tmp_path, monkeypatch, capsys):
    """A directory under evolutions/ with no EVOLUTION.md is reported."""
    import importlib.util

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app").mkdir()
    (repo / "evolutions" / "orphan-no-manifest").mkdir(parents=True)
    (repo / "evolutions" / "complete-bundle").mkdir(parents=True)
    (repo / "evolutions" / "complete-bundle" / "EVOLUTION.md").write_text("# x\n")

    monkeypatch.setenv("GEN_DOCS_ROOT", str(repo))

    spec = importlib.util.spec_from_file_location(
        "gen_docs_test_copy",
        os.path.abspath("scripts/gen_docs.py"),
    )
    gen_docs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen_docs)

    gen_docs.main([])

    captured = capsys.readouterr()
    assert "orphan evolution" in captured.err
    assert "orphan-no-manifest" in captured.err
    assert "complete-bundle" not in captured.err
