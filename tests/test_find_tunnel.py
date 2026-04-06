import subprocess
import pytest
import re

@pytest.mark.infra
def test_get_tunnel_url():
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", "50", "bezos-tunnel"],
            capture_output=True, text=True, timeout=15
        )
        combined = result.stdout + result.stderr
        urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", combined)

        if urls:
            pytest.fail(f"CURRENT_TUNNEL_URL: {urls[-1]}")
        else:
            pytest.fail(f"URL_NOT_FOUND_IN_LOGS. Output preview: {combined[-500:]}")
    except Exception as e:
        pytest.fail(f"EXECUTION_ERROR: {str(e)}")
