import subprocess
import pytest
import re

def test_get_tunnel_url():
    # Attempt to get the latest URL from the docker logs of the tunnel
    # We use 'tail -n 50' to ensure we see the most recent startup sequence
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", "50", "bezos-tunnel"],
            capture_output=True, text=True, timeout=15
        )
        # Search in both stderr (standard for cloudflared) and stdout
        combined = result.stdout + result.stderr
        urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", combined)
        
        if urls:
            # The last one in the logs is usually the current one
            pytest.fail(f"CURRENT_TUNNEL_URL: {urls[-1]}")
        else:
            pytest.fail(f"URL_NOT_FOUND_IN_LOGS. Output preview: {combined[-500:]}")
    except Exception as e:
        pytest.fail(f"EXECUTION_ERROR: {str(e)}")
