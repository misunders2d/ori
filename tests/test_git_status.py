import subprocess
import os
import pytest

def test_git_status():
    def run(cmd):
        res = subprocess.run(cmd, capture_output=True, text=True, shell=True)
        print(f"\nCMD: {cmd}")
        print(f"STDOUT: {res.stdout}")
        print(f"STDERR: {res.stderr}")
    
    run("git status")
    run("git branch -a")
    run("git remote -v")
    run("git log -n 5 --oneline")
    run("git diff --name-only")
    run("ls -la app/app_utils/config.py")
    with open("app/app_utils/config.py", "r") as f:
        print(f"Content length: {len(f.read())}")
    assert True
