import subprocess
import pytest

@pytest.mark.infra
def test_docker_status():
    try:
        res = subprocess.run(["docker", "ps", "--all"], capture_output=True, text=True, timeout=10)
        pytest.fail(f"DOCKER_PS_ALL:\n{res.stdout}")
    except Exception as e:
        pytest.fail(f"ERROR: {e}")
