import os
import subprocess
import pwd
import grp
import pytest

def test_check_permissions():
    def check_path(path):
        res = f"--- Checking {path} ---\n"
        try:
            stat = os.stat(path)
            res += f"UID: {stat.st_uid} ({pwd.getpwuid(stat.st_uid).pw_name})\n"
            res += f"GID: {stat.st_gid} ({grp.getgrgid(stat.st_gid).gr_name})\n"
            res += f"Mode: {oct(stat.st_mode)}\n"
            res += f"Writeable by current user: {os.access(path, os.W_OK)}\n"
        except Exception as e:
            res += f"Error: {e}\n"
        return res

    paths = [
        "/code",
        "/code/.gitignore",
        "/code/data",
        "/code/data/agent.log",
        "/code/.git",
        "/home/agentuser",
        "/home/agentuser/.cache",
    ]

    content = ""
    for p in paths:
        content += check_path(p)

    content += "\nCurrent User Info:\n"
    content += f"UID: {os.getuid()}\n"
    content += f"GID: {os.getgid()}\n"
    content += f"Effective UID: {os.geteuid()}\n"

    content += "\nGit Config:\n"
    try:
        content += subprocess.check_output(["git", "config", "--global", "--list"], text=True)
    except Exception as e:
        content += f"Error checking git config: {e}\n"

    content += "\nGit Status:\n"
    try:
        content += subprocess.check_output(["git", "status"], text=True)
    except Exception as e:
        content += f"Error checking git status: {e}\n"
    
    with open("/code/data/debug_perms.log", "w") as f:
        f.write(content)
    
    assert True
