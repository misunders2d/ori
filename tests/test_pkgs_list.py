import subprocess
import sys

def test_list_pkgs():
    result = subprocess.run([sys.executable, "-m", "pip", "list"], capture_output=True, text=True)
    print("\n--- PIP LIST ---")
    print(result.stdout)
    # Check for google-* packages
    pkgs = [line.split()[0] for line in result.stdout.splitlines()[2:] if line.strip()]
    google_pkgs = [p for p in pkgs if p.lower().startswith("google")]
    print("\n--- GOOGLE PKGS ---")
    print(google_pkgs)
    assert True
