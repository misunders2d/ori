import subprocess
import os

def test_deploy_sh_syntax():
    # Check if deploy.sh exists in sandbox
    path = "deploy.sh"
    if os.path.exists(path):
        result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        assert result.returncode == 0, f"Syntax error in deploy.sh: {result.stderr}"

def test_start_sh_syntax():
    path = "start.sh"
    if os.path.exists(path):
        result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        assert result.returncode == 0, f"Syntax error in start.sh: {result.stderr}"

def test_rollback_sh_syntax():
    path = "rollback.sh"
    if os.path.exists(path):
        result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        assert result.returncode == 0, f"Syntax error in rollback.sh: {result.stderr}"
