import subprocess
import re
import shutil

import pytest


@pytest.mark.infra
def test_find_cloudflare_url():
    if not shutil.which("docker"):
        pytest.skip("docker CLI not installed")

    result = subprocess.run(
        ["docker", "logs", "bezos-tunnel"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 and "No such container" in result.stderr:
        pytest.skip("bezos-tunnel container not running")

    combined = result.stderr + result.stdout
    match = re.search(r"(https://[a-z0-9-]+\.trycloudflare\.com)", combined)

    assert match, f"NO_URL_IN_LOGS: {combined[-500:]}"
