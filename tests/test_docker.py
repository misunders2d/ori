import shutil
import subprocess

import pytest


@pytest.mark.infra
def test_docker_status():
    if not shutil.which("docker"):
        pytest.skip("docker CLI not installed")

    res = subprocess.run(
        ["docker", "ps", "--all"], capture_output=True, text=True, timeout=10
    )
    assert res.returncode == 0, res.stderr or res.stdout
