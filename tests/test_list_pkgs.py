import subprocess
import sys

def test_list_pkgs():
    result = subprocess.run([sys.executable, "-m", "pip", "list"], capture_output=True, text=True)
    print("\n--- PIP LIST ---")
    print(result.stdout)
    assert "google" in result.stdout
