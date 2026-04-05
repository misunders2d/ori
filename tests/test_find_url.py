import subprocess
import re
import pytest

def test_find_cloudflare_url():
    try:
        # Container is bezos-tunnel
        result = subprocess.run(
            ["docker", "logs", "bezos-tunnel"],
            capture_output=True, text=True, timeout=10
        )
        logs = result.stderr
        match = re.search(r"(https://[a-z0-9-]+\.trycloudflare\.com)", logs)
        if match:
            pytest.fail(f"URL_FOUND: {match.group(1)}")
        
        # Check stdout
        match = re.search(r"(https://[a-z0-9-]+\.trycloudflare\.com)", result.stdout)
        if match:
            pytest.fail(f"URL_FOUND: {match.group(1)}")
            
        pytest.fail(f"NO_URL_IN_LOGS: {logs[-500:]}")
    except Exception as e:
        pytest.fail(f"ERROR: {e}")
