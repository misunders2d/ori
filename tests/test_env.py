import os
import pytest

def test_print_env():
    print(f"DEBUG_ALLOWED_USER_IDS: {os.environ.get('ALLOWED_USER_IDS')}")
    print(f"DEBUG_ADMIN_USER_IDS: {os.environ.get('ADMIN_USER_IDS')}")
    assert True
