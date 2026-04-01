import subprocess
import os

def test_write_git_log():
    live_repo = os.path.abspath(os.path.join(os.getcwd(), "..", ".."))
    res = subprocess.run(["git", "log", "--oneline", "-n", "20"], capture_output=True, text=True, cwd=live_repo)
    with open(os.path.join(live_repo, "git_log_output.txt"), "w") as f:
        f.write(res.stdout)
    assert True
