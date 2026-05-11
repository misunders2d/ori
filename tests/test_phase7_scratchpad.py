"""Phase 7 unit tests: scratchpad owner tagging + session retention sweep."""

import os
import time

import pytest


# ---------------------------------------------------------------------------
# Owner tagging
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, session_id: str):
        self.session_id = session_id


class _FakeAgent:
    def __init__(self, name: str):
        self.name = name


class _FakeInvocationContext:
    def __init__(self, agent_name: str):
        self.agent = _FakeAgent(agent_name)


class _FakeToolContext:
    """Minimal stand-in for ADK's ToolContext."""

    def __init__(self, session_id: str = "sess-1", agent_name: str = ""):
        self.session = _FakeSession(session_id)
        if agent_name:
            self._invocation_context = _FakeInvocationContext(agent_name)
        # tool_context.state is touched by some scratchpad guardrail call
        # sites but not the helpers we exercise here.


@pytest.fixture
def isolated_pad_dir(tmp_path, monkeypatch):
    """Redirect the scratchpad root to a tmp dir for the test."""
    pad_root = tmp_path / "scratchpads"
    from app.tools import scratchpad
    monkeypatch.setattr(scratchpad, "_SCRATCHPAD_DIR", str(pad_root))
    return pad_root


def test_write_with_owner_tags_filename(isolated_pad_dir):
    from app.tools.scratchpad import scratchpad_write

    ctx = _FakeToolContext(session_id="sess-A", agent_name="AmazonAgent")
    result = scratchpad_write("research", "first finding", tool_context=ctx)

    assert result["status"] == "success"
    assert result["owner"] == "AmazonAgent"
    expected = isolated_pad_dir / "sess-A" / "AmazonAgent__research.md"
    assert expected.is_file()


def test_write_without_owner_falls_back_to_legacy_path(isolated_pad_dir):
    from app.tools.scratchpad import scratchpad_write

    # No agent name detected.
    ctx = _FakeToolContext(session_id="sess-B")
    result = scratchpad_write("note", "legacy content", tool_context=ctx)

    assert result["owner"] == ""
    expected = isolated_pad_dir / "sess-B" / "note.md"
    assert expected.is_file()


def test_read_prefers_owner_tagged_over_legacy(isolated_pad_dir):
    from app.tools.scratchpad import scratchpad_read

    sess = isolated_pad_dir / "sess-C"
    sess.mkdir(parents=True)
    (sess / "AmazonAgent__shared.md").write_text("owner-tagged content")
    (sess / "shared.md").write_text("legacy content")

    ctx = _FakeToolContext(session_id="sess-C", agent_name="AmazonAgent")
    result = scratchpad_read("shared", tool_context=ctx)

    assert result["status"] == "success"
    assert result["content"] == "owner-tagged content"


def test_read_falls_back_to_legacy_when_no_owner_match(isolated_pad_dir):
    """A pad written before owner tagging existed stays readable."""
    from app.tools.scratchpad import scratchpad_read

    sess = isolated_pad_dir / "sess-D"
    sess.mkdir(parents=True)
    (sess / "old.md").write_text("legacy-only pad")

    # Agent attempts a read with its own owner — should fall through to legacy.
    ctx = _FakeToolContext(session_id="sess-D", agent_name="CoordinatorAgent")
    result = scratchpad_read("old", tool_context=ctx)

    assert result["status"] == "success"
    assert result["content"] == "legacy-only pad"


def test_list_returns_owner_per_entry(isolated_pad_dir):
    from app.tools.scratchpad import scratchpad_list

    sess = isolated_pad_dir / "sess-E"
    sess.mkdir(parents=True)
    (sess / "AmazonAgent__a.md").write_text("x")
    (sess / "CoordinatorAgent__b.md").write_text("y")
    (sess / "legacy.md").write_text("z")  # untagged

    ctx = _FakeToolContext(session_id="sess-E")
    result = scratchpad_list(tool_context=ctx)

    by_name = {p["name"]: p for p in result["scratchpads"]}
    assert by_name["a"]["owner"] == "AmazonAgent"
    assert by_name["b"]["owner"] == "CoordinatorAgent"
    assert by_name["legacy"]["owner"] == ""


def test_list_with_owner_filter(isolated_pad_dir):
    from app.tools.scratchpad import scratchpad_list

    sess = isolated_pad_dir / "sess-F"
    sess.mkdir(parents=True)
    (sess / "AmazonAgent__hit.md").write_text("x")
    (sess / "CoordinatorAgent__miss.md").write_text("y")
    (sess / "miss.md").write_text("z")  # legacy, no owner tag

    ctx = _FakeToolContext(session_id="sess-F")
    result = scratchpad_list(tool_context=ctx, owner="AmazonAgent")

    names = sorted(p["name"] for p in result["scratchpads"])
    assert names == ["hit"]


def test_write_then_clear_removes_owner_tagged_file(isolated_pad_dir):
    from app.tools.scratchpad import scratchpad_clear, scratchpad_write

    ctx = _FakeToolContext(session_id="sess-G", agent_name="AmazonAgent")
    scratchpad_write("temp", "data", tool_context=ctx)

    path = isolated_pad_dir / "sess-G" / "AmazonAgent__temp.md"
    assert path.is_file()

    scratchpad_clear("temp", tool_context=ctx)
    assert not path.exists()


def test_write_appends_to_existing_owner_tagged_file(isolated_pad_dir):
    """Writes go to the same file (don't fragment) when one already exists."""
    from app.tools.scratchpad import scratchpad_write

    ctx = _FakeToolContext(session_id="sess-H", agent_name="AmazonAgent")
    scratchpad_write("notes", "first", tool_context=ctx)
    scratchpad_write("notes", "second", tool_context=ctx)

    contents = (isolated_pad_dir / "sess-H" / "AmazonAgent__notes.md").read_text()
    assert "first" in contents
    assert "second" in contents


def test_owner_arg_overrides_detection(isolated_pad_dir):
    from app.tools.scratchpad import scratchpad_write

    ctx = _FakeToolContext(session_id="sess-I", agent_name="AmazonAgent")
    result = scratchpad_write("x", "data", tool_context=ctx, owner="CoordinatorAgent")

    assert result["owner"] == "CoordinatorAgent"
    assert (isolated_pad_dir / "sess-I" / "CoordinatorAgent__x.md").is_file()


def test_owner_sanitization(isolated_pad_dir):
    """A weird agent name with spaces/dots gets normalized to underscores."""
    from app.tools.scratchpad import scratchpad_write

    ctx = _FakeToolContext(session_id="sess-J", agent_name="Some Weird Name.v2")
    result = scratchpad_write("x", "data", tool_context=ctx)

    assert result["owner"] == "Some_Weird_Name_v2"
    assert (isolated_pad_dir / "sess-J" / "Some_Weird_Name_v2__x.md").is_file()


# ---------------------------------------------------------------------------
# Session retention sweep
# ---------------------------------------------------------------------------


def test_sweep_scratchpad_sessions_removes_stale_dirs(tmp_path):
    from app.app_utils.tmp_sweeper import sweep_scratchpad_sessions

    root = tmp_path / "scratchpads"
    root.mkdir()

    fresh = root / "fresh-session"
    stale = root / "stale-session"
    fresh.mkdir()
    stale.mkdir()
    (fresh / "a.md").write_text("active")
    (stale / "b.md").write_text("old")

    # Backdate the stale dir mtime by 30 days.
    old_time = time.time() - (30 * 24 * 3600)
    os.utime(stale, (old_time, old_time))

    removed = sweep_scratchpad_sessions(root=str(root), ttl_days=7)

    assert removed == 1
    assert fresh.exists()
    assert not stale.exists()


def test_sweep_scratchpad_sessions_noop_on_empty_or_missing(tmp_path):
    from app.app_utils.tmp_sweeper import sweep_scratchpad_sessions

    # Missing root entirely.
    assert sweep_scratchpad_sessions(root=str(tmp_path / "absent"), ttl_days=7) == 0

    # Empty root.
    (tmp_path / "scratchpads").mkdir()
    assert sweep_scratchpad_sessions(root=str(tmp_path / "scratchpads"), ttl_days=7) == 0


def test_sweep_respects_ttl_env_var(tmp_path, monkeypatch):
    from app.app_utils.tmp_sweeper import sweep_scratchpad_sessions

    root = tmp_path / "scratchpads"
    root.mkdir()
    dir1 = root / "s1"
    dir1.mkdir()
    # Backdate by 2 days.
    age = time.time() - (2 * 24 * 3600)
    os.utime(dir1, (age, age))

    # TTL = 1 day → swept.
    monkeypatch.setenv("SCRATCHPAD_SESSION_TTL_DAYS", "1")
    assert sweep_scratchpad_sessions(root=str(root)) == 1
    assert not dir1.exists()


def test_sweep_respects_ttl_env_var_keeps_fresh(tmp_path, monkeypatch):
    from app.app_utils.tmp_sweeper import sweep_scratchpad_sessions

    root = tmp_path / "scratchpads"
    root.mkdir()
    dir1 = root / "s1"
    dir1.mkdir()
    # 12-hour-old dir.
    age = time.time() - (12 * 3600)
    os.utime(dir1, (age, age))

    # TTL = 1 day → keep.
    monkeypatch.setenv("SCRATCHPAD_SESSION_TTL_DAYS", "1")
    assert sweep_scratchpad_sessions(root=str(root)) == 0
    assert dir1.exists()
