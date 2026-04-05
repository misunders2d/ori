import subprocess
import pytest

def test_inspect_docker():
    try:
        # Run docker ps to see all containers
        result = subprocess.run(
            ["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=10
        )
        # Fail with the output to see it
        pytest.fail(f"\nDOCKER_PS_OUTPUT:\n{result.stdout}")
    except Exception as e:
        pytest.fail(f"ERROR: {e}")
