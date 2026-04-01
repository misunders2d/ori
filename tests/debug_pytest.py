import pytest
import sys
import os

if __name__ == "__main__":
    # Add current dir to path
    sys.path.insert(0, os.getcwd())
    # Run pytest on the specific file and capture output
    exit_code = pytest.main(["tests/test_identity_permissions.py", "-vv"])
    sys.exit(exit_code)
