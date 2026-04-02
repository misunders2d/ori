import subprocess
import os

def run(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    print(f"CMD: {cmd}")
    print(f"STDOUT: {res.stdout}")
    print(f"STDERR: {res.stderr}")

run("git fetch origin master")
run("git reset --hard origin/master")
run("git clean -fd --exclude=data --exclude=.env")
