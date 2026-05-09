import subprocess
import re
import shutil

import pytest


@pytest.mark.infra
def test_get_tunnel_url():
    if not shutil.which("docker"):
        pytest.skip("docker CLI not installed")

    result = subprocess.run(
        ["docker", "logs", "--tail", "50", "bezos-tunnel"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0 and "No such container" in result.stderr:
        pytest.skip("bezos-tunnel container not running")

    combined = result.stdout + result.stderr
    urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", combined)
    assert urls, f"URL_NOT_FOUND_IN_LOGS. Output preview: {combined[-500:]}"
