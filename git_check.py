import subprocess

def check_git():
    try:
        # Check remote URL
        remote = subprocess.run(["git", "remote", "-v"], capture_output=True, text=True)
        print("--- REMOTES ---")
        print(remote.stdout)
        
        # Check last 5 commits
        log = subprocess.run(["git", "log", "-n", "5", "--oneline"], capture_output=True, text=True)
        print("\n--- LAST 5 COMMITS ---")
        print(log.stdout)
        
        # Check status
        status = subprocess.run(["git", "status"], capture_output=True, text=True)
        print("\n--- STATUS ---")
        print(status.stdout)
    except Exception as e:
        print(f"Error: {e}")

check_git()
