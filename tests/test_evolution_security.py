from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class MockTool:
    def __init__(self, name):
        self.name = name


def _tool_context(user_id="admin_user", session_id="session-1"):
    context = MagicMock()
    context.state.to_dict.return_value = {
        "user_id": user_id,
        "session_id": session_id,
    }
    context.session = SimpleNamespace(session_id=session_id)
    return context


def test_imported_evolution_cannot_apply_to_live_tree(tmp_path, monkeypatch):
    from app.tools import evolution_catalog

    project_root = tmp_path / "project"
    evolutions_dir = project_root / "evolutions"
    monkeypatch.setattr(evolution_catalog, "PROJECT_ROOT", str(project_root))
    monkeypatch.setattr(evolution_catalog, "EVOLUTIONS_DIR", str(evolutions_dir))

    result = evolution_catalog.evolution_import(
        name="remote-fix",
        manifest="---\nname: remote-fix\n---\n",
        files={"app/tools/remote.py": "print('unsafe')"},
        apply=True,
    )

    assert result["status"] == "error"
    assert "Direct live apply is blocked" in result["message"]
    assert not (project_root / "app" / "tools" / "remote.py").exists()


def test_imported_evolution_rejects_path_traversal(tmp_path, monkeypatch):
    from app.tools import evolution_catalog

    project_root = tmp_path / "project"
    evolutions_dir = project_root / "evolutions"
    monkeypatch.setattr(evolution_catalog, "PROJECT_ROOT", str(project_root))
    monkeypatch.setattr(evolution_catalog, "EVOLUTIONS_DIR", str(evolutions_dir))

    result = evolution_catalog.evolution_import(
        name="remote-fix",
        manifest="---\nname: remote-fix\n---\n",
        files={"../escape.py": "print('escape')"},
    )

    assert result["status"] == "error"
    assert "Path traversal denied" in result["message"]
    assert not (project_root / "escape.py").exists()


def test_admin_guardrail_stages_secret_and_destructive_tools():
    from app.callbacks.guardrails import admin_tool_guardrail

    protected_tools = [
        "get_my_a2a_key",
        "evolution_git_pull",
        "evolution_git_reset",
        "evolution_sync_local_to_upstream",
    ]

    with patch.dict(
        "os.environ",
        {"ADMIN_USER_IDS": "admin_user", "REQUIRE_2FA": "false"},
        clear=False,
    ):
        with patch("app.core.pending_actions.stage_action", return_value="ACT-TEST"):
            for tool_name in protected_tools:
                result = admin_tool_guardrail(
                    MockTool(tool_name), {}, _tool_context()
                )
                assert result["status"] == "error"
                assert "Approve ACT-TEST" in result["message"]


@pytest.mark.asyncio
async def test_execute_approved_action_rejects_user_or_session_mismatch():
    from app.tools.system import execute_approved_action

    action = {
        "tool_name": "session_refresh",
        "args": {"mode": "fresh"},
        "user_id": "admin_user",
        "session_id": "session-1",
    }

    with patch.dict("os.environ", {"REQUIRE_2FA": "false"}, clear=False):
        with patch("app.core.pending_actions.get_and_delete_action", return_value=action):
            result = await execute_approved_action(
                "ACT-TEST", tool_context=_tool_context(user_id="other")
            )
            assert result["status"] == "error"
            assert "does not belong to this user" in result["message"]

        with patch("app.core.pending_actions.get_and_delete_action", return_value=action):
            result = await execute_approved_action(
                "ACT-TEST", tool_context=_tool_context(session_id="other-session")
            )
            assert result["status"] == "error"
            assert "does not belong to this session" in result["message"]


def test_evolution_commit_requires_matching_pytest_verification(tmp_path, monkeypatch):
    from app.tools import evolution

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(evolution, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(evolution, "_is_child_container", lambda: False)
    sandbox_file = tmp_path / "data" / "sandbox" / "app" / "tools" / "new_tool.py"
    sandbox_file.parent.mkdir(parents=True)
    sandbox_file.write_text("VALUE = 1\n")

    context = SimpleNamespace(state={})
    result = evolution.evolution_commit_and_push(
        "fix: should not commit", tool_context=context
    )

    assert result["status"] == "error"
    assert "successful pytest verification" in result["message"]
