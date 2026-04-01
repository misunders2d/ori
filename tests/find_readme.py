import subprocess
import os

def test_find_readme():
    repo_path = os.path.abspath(os.path.join(os.getcwd(), "..", ".."))
    # Search for "Manual Installation" in README.md history
    result = subprocess.run(["git", "log", "-GManual Installation", "--oneline", "--", "README.md"], capture_output=True, text=True, cwd=repo_path)
    print(f"\nCommits touching 'Manual Installation':\n{result.stdout}")
    
    # Search for "curl" in README.md history
    result = subprocess.run(["git", "log", "-Gcurl", "--oneline", "--", "README.md"], capture_output=True, text=True, cwd=repo_path)
    print(f"\nCommits touching 'curl':\n{result.stdout}")
    
    assert False
