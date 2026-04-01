import subprocess
import os

def test_search_curl():
    repo_path = os.path.abspath(os.path.join(os.getcwd(), "..", ".."))
    # Find commits that contain 'curl' in README.md
    result = subprocess.run(["git", "log", "-S", "curl", "--oneline", "--", "README.md"], capture_output=True, text=True, cwd=repo_path)
    print("\nCommits touching 'curl':\n" + result.stdout)
    
    # Also just list all README commits to be safe
    result = subprocess.run(["git", "log", "--oneline", "--", "README.md"], capture_output=True, text=True, cwd=repo_path)
    print("\nAll README commits:\n" + result.stdout)
    assert False
