import subprocess
import os
import pytest

def test_git_output():
    def run(cmd):
        res = subprocess.run(cmd, capture_output=True, text=True, shell=True)
        return f"\nCMD: {cmd}\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}\n"
    
    out = run("git status")
    out += run("git rev-parse HEAD")
    with open("data/git_status_test.txt", "w") as f:
        f.write(out)
    assert True
