import shutil
import os

def test_fix_sandbox():
    for path in ["data", "tests/cleanup.py", "tests/test_cleanup.py", "tests/test_a2a_import.py", ".gitignore"]:
        if os.path.exists(path):
            if os.path.isdir(path):
                shutil.rmtree(path)
                print(f"Deleted directory {path}")
            else:
                os.remove(path)
                print(f"Deleted file {path}")
