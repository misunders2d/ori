import subprocess
import os

def run():
    try:
        res = subprocess.run(["git", "branch", "-a"], capture_output=True, text=True)
        print("Branches:\n", res.stdout)
        
        res = subprocess.run(["git", "log", "-n", "5", "--oneline", "--all"], capture_output=True, text=True)
        print("\nRecent Commits (all branches):\n", res.stdout)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    run()
