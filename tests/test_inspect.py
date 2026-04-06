import subprocess
import pytest

@pytest.mark.infra
def test_inspect_docker():
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=10
        )
        pytest.fail(f"\nDOCKER_PS_OUTPUT:\n{result.stdout}")
    except Exception as e:
        pytest.fail(f"ERROR: {e}")
