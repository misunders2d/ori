import subprocess
import os

def test_get_curl_commit():
    repo_path = os.path.abspath(os.path.join(os.getcwd(), "..", ".."))
    # Find the specific commit hash that added/contained 'curl'
    result = subprocess.run(["git", "log", "-Gcurl", "--oneline", "-n", "1", "--", "README.md"], capture_output=True, text=True, cwd=repo_path)
    sha = result.stdout.split()[0] if result.stdout else "NOT_FOUND"
    
    if sha != "NOT_FOUND":
        content = subprocess.run(["git", "show", f"{sha}:README.md"], capture_output=True, text=True, cwd=repo_path)
        print(f"\nSHA:{sha}\n{content.stdout}")
    else:
        print("CURL NOT FOUND IN HISTORY")
    assert False
