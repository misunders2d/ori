import subprocess
import os

def test_find_curl_readme():
    repo_path = os.path.abspath(os.path.join(os.getcwd(), "..", ".."))
    # Find commits that contain 'curl' in README.md
    # We use git log -G to search for the string in the diff
    result = subprocess.run(["git", "log", "-Gcurl", "--oneline", "--", "README.md"], capture_output=True, text=True, cwd=repo_path)
    # Also search git rev-list to find all commits touching it
    # And then check each one
    commits = result.stdout.strip().split("\n")
    print(f"\nCommits containing 'curl':\n{result.stdout}")
    
    if commits:
        last_curl_commit = commits[0].split()[0]
        content = subprocess.run(["git", "show", f"{last_curl_commit}:README.md"], capture_output=True, text=True, cwd=repo_path)
        print(f"\n--- CONTENT FROM {last_curl_commit} ---\n{content.stdout[:500]}...")
    
    assert False
