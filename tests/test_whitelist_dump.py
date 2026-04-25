import os
import pytest
from app.runtime.perimeter import get_whitelist, get_blacklist

def test_whitelist_dump():
    print(f"\nWhitelist content: {get_whitelist()}")
    print(f"Blacklist content: {get_blacklist()}")
    print(f"ALLOWED_USER_IDS: {os.environ.get('ALLOWED_USER_IDS')}")
    print(f"ADMIN_USER_IDS: {os.environ.get('ADMIN_USER_IDS')}")
    assert True
