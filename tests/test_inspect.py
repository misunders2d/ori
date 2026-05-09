import shutil
import subprocess

import pytest


@pytest.mark.infra
def test_inspect_docker():
    if not shutil.which("docker"):
        pytest.skip("docker CLI not installed")

    result = subprocess.run(
        ["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout
