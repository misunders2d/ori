import subprocess

def test_netstat():
    # Use ss or netstat to see what is listening
    try:
        # Try ss first (modern Linux)
        res = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
        print(f"\nSS_OUTPUT:\n{res.stdout}")
        
        # Also check docker containers
        res = subprocess.run(["docker", "ps", "--all"], capture_output=True, text=True, timeout=5)
        print(f"\nDOCKER_PS_ALL:\n{res.stdout}")
    except Exception as e:
        print(f"ERROR: {e}")
