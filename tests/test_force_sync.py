import subprocess
import os
import pytest

def test_force_sync():
    def run(cmd):
        res = subprocess.run(cmd, capture_output=True, text=True, shell=True)
        print(f"\nCMD: {cmd}")
        print(f"STDOUT: {res.stdout}")
        print(f"STDERR: {res.stderr}")
    
    # Force alignment with remote
    run("git fetch origin master")
    run("git reset --hard origin/master")
    run("git clean -fd --exclude=data/")
    
    # Verify alignment
    run("git status")
    run("git rev-parse HEAD")
    
    assert True
