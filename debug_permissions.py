import os
import subprocess
import pwd
import grp

def check_path(path):
    print(f"--- Checking {path} ---")
    try:
        stat = os.stat(path)
        print(f"UID: {stat.st_uid} ({pwd.getpwuid(stat.st_uid).pw_name})")
        print(f"GID: {stat.st_gid} ({grp.getgrgid(stat.st_gid).gr_name})")
        print(f"Mode: {oct(stat.st_mode)}")
        print(f"Writeable by current user: {os.access(path, os.W_OK)}")
    except Exception as e:
        print(f"Error: {e}")

paths = [
    "/code",
    "/code/.gitignore",
    "/code/data",
    "/code/data/agent.log",
    "/code/.git",
    "/home/agentuser",
    "/home/agentuser/.cache",
]

for p in paths:
    check_path(p)

print("\nCurrent User Info:")
print(f"UID: {os.getuid()}")
print(f"GID: {os.getgid()}")
print(f"Effective UID: {os.geteuid()}")

print("\nGit Config:")
try:
    print(subprocess.check_output(["git", "config", "--global", "--list"], text=True))
except Exception as e:
    print(f"Error checking git config: {e}")

print("\nGit Status:")
try:
    print(subprocess.check_output(["git", "status"], text=True))
except Exception as e:
    print(f"Error checking git status: {e}")
