import os
import subprocess

def test_github_token():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is NOT set.")
    else:
        print(f"GITHUB_TOKEN is set (length: {len(token)}).")

def test_npx_available():
    try:
        result = subprocess.run(["npx", "--version"], capture_output=True, text=True, check=True)
        print(f"npx is available: {result.stdout.strip()}")
    except Exception as e:
        print(f"npx is NOT available or failed: {e}")

if __name__ == "__main__":
    test_github_token()
    test_npx_available()
