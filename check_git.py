import subprocess
import os

def run(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    print(f"CMD: {cmd}")
    print(f"STDOUT: {res.stdout}")
    print(f"STDERR: {res.stderr}")
    print("-" * 20)

run("git status")
run("git diff --name-only")
run("git log -n 5 --oneline")
