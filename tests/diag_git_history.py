import subprocess
import os

def test_print_git_log():
    live_repo = os.path.abspath(os.path.join(os.getcwd(), "..", ".."))
    res = subprocess.run(["git", "log", "--oneline", "-n", "20"], capture_output=True, text=True, cwd=live_repo)
    raise ValueError(f"\n--- RECENT COMMIT HISTORY ---\n{res.stdout}")
