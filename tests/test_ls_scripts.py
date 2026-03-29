import os
def test_list_scripts():
    path = "scripts"
    # If it's a sandbox, we might need to check if we staged it
    if not os.path.exists(path):
        print(f"\nDEBUG: scripts directory missing at {os.getcwd()}")
        # Check if we can find it elsewhere?
        # No, let's just assert it exists now that we staged a dummy file
    assert os.path.exists(path), f"scripts directory not found starting from {os.getcwd()}"
    files = os.listdir(path)
    assert len(files) > 0, "Scripts directory is empty"
