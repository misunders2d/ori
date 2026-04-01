import subprocess
import os

def test_find_the_real_one():
    repo_path = os.path.abspath(os.path.join(os.getcwd(), "..", ".."))
    # Search all commits for "curl" in the README
    result = subprocess.run(["git", "log", "-Gcurl", "--oneline", "--", "README.md"], capture_output=True, text=True, cwd=repo_path)
    commits = result.stdout.strip().split("\n")
    print(f"\nCOMMITS WITH CURL:\n{result.stdout}")
    
    for c_line in commits:
        if not c_line: continue
        sha = c_line.split()[0]
        content = subprocess.run(["git", "show", f"{sha}:README.md"], capture_output=True, text=True, cwd=repo_path)
        if "curl" in content.stdout:
            print(f"\n--- FOUND IN {sha} ---\n{content.stdout[:1000]}")
            break
    assert False
