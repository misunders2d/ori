import subprocess
import re
import pytest
import socket

@pytest.mark.infra
def test_check_local_and_tunnel():
    # 1. Check if the agent is actually listening on 8002
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        listening = (s.connect_ex(('localhost', 8002)) == 0)

    # 2. Get latest tunnel URL
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", "200", "bezos-tunnel"],
            capture_output=True, text=True, timeout=15
        )
        logs = result.stdout + result.stderr
        urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", logs)
        url = urls[-1] if urls else "NOT_FOUND"
    except Exception as e:
        url = f"ERROR: {e}"
        logs = ""

    # 3. Report
    pytest.fail(f"LISTEN_8002: {listening} | URL: {url} | LOG_END: {logs[-300:]}")
