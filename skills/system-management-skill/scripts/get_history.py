import subprocess
import os

def run_git(args):
    try:
        res = subprocess.run(["git"] + args, capture_output=True, text=True)
        print(f"--- git {' '.join(args)} ---")
        print(res.stdout)
        if res.stderr:
            print(f"ERR: {res.stderr}")
    except Exception as e:
        print(f"FAILED: {e}")

if __name__ == "__main__":
    run_git(["log", "--oneline", "-n", "20"])
