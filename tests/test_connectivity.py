import subprocess
import re
import shutil
import socket

import pytest


@pytest.mark.infra
def test_check_local_and_tunnel():
    # 1. Check if the agent is actually listening on 8002
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            listening = s.connect_ex(("localhost", 8002)) == 0
    except PermissionError as exc:
        pytest.skip(f"socket checks not permitted: {exc}")

    # 2. Get latest tunnel URL
    if not shutil.which("docker"):
        pytest.skip("docker CLI not installed")

    result = subprocess.run(
        ["docker", "logs", "--tail", "200", "bezos-tunnel"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0 and "No such container" in result.stderr:
        pytest.skip("bezos-tunnel container not running")

    logs = result.stdout + result.stderr
    urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", logs)

    # 3. Report
    assert listening, "agent is not listening on localhost:8002"
    assert urls, f"tunnel URL not found in logs: {logs[-300:]}"
