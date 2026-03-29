import subprocess
import sys

def test_check_pkg():
    result = subprocess.run([sys.executable, "-m", "pip", "show", "google-adk"], capture_output=True, text=True)
    print("\n--- PIP SHOW GOOGLE-ADK ---")
    print(result.stdout)
    assert "Name: google-adk" in result.stdout
