import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from app.core.health import get_system_health
import subprocess

@pytest.mark.asyncio
async def test_get_system_health_git_synced():
    def side_effect(cmd, *args, **kwargs):
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        if "diff-index" in cmd:
            m.returncode = 0
        elif "rev-parse" in cmd:
            if "--abbrev-ref" in cmd:
                m.stdout = "master\n"
            elif "origin/master" in cmd:
                m.stdout = "abc1234\n"
            else:
                m.stdout = "abc1234\n"
        return m

    with patch("subprocess.run", side_effect=side_effect):
        with patch("os.path.exists", return_value=False):
            with patch("shutil.disk_usage", return_value=(100, 10, 90)):
                with patch("google.genai.Client") as mock_client_class:
                    mock_client = mock_client_class.return_value
                    mock_client.aio.models.list = AsyncMock()
                    report = await get_system_health()
                    assert report["vitals"]["git_integrity"] == "synced"
                    assert report["vitals"]["version_hash"] == "abc1234"

@pytest.mark.asyncio
async def test_get_system_health_git_modified():
    def side_effect(cmd, *args, **kwargs):
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = 0
        m.stdout = ""
        if "diff-index" in cmd:
            m.returncode = 1
        elif "rev-parse" in cmd:
            if "--abbrev-ref" in cmd:
                m.stdout = "master\n"
            else:
                m.stdout = "abc1234\n"
        return m

    with patch("subprocess.run", side_effect=side_effect):
        with patch("os.path.exists", return_value=False):
            with patch("shutil.disk_usage", return_value=(100, 10, 90)):
                with patch("google.genai.Client") as mock_client_class:
                    mock_client = mock_client_class.return_value
                    mock_client.aio.models.list = AsyncMock()
                    report = await get_system_health()
                    assert report["vitals"]["git_integrity"] == "synced (modified)"

@pytest.mark.asyncio
async def test_get_system_health_git_ahead():
    def side_effect(cmd, *args, **kwargs):
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = 0
        m.stdout = ""
        if "diff-index" in cmd:
            m.returncode = 0
        elif "rev-parse" in cmd:
            if "--abbrev-ref" in cmd:
                m.stdout = "master\n"
            elif "origin/master" in cmd:
                m.stdout = "xyz7890\n"
            else:
                m.stdout = "abc1234\n"
        elif "rev-list" in cmd:
            m.stdout = "2\t0\n"
        return m

    with patch("subprocess.run", side_effect=side_effect):
        with patch("os.path.exists", return_value=False):
            with patch("shutil.disk_usage", return_value=(100, 10, 90)):
                with patch("google.genai.Client") as mock_client_class:
                    mock_client = mock_client_class.return_value
                    mock_client.aio.models.list = AsyncMock()
                    report = await get_system_health()
                    assert report["vitals"]["git_integrity"] == "ahead_by_2"
