import os
import subprocess

def test_wipe_sandbox():
    # Attempt to use git to unstage ghost files
    # Note: we might not have 'git' in the test environment, but let's try
    try:
        subprocess.run(['git', 'reset'], check=False)
        subprocess.run(['git', 'rm', '--cached', 'data/pending_actions.db', 'data/agent.json'], check=False)
    except Exception:
        pass
    
    # Try to delete the ghost files if they exist
    for f in ['data/pending_actions.db', 'data/agent.json']:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
    
    # Also clear the __pycache__ just in case
    subprocess.run(['find', '.', '-name', '__pycache__', '-exec', 'rm', '-rf', '{}', '+'], check=False)
