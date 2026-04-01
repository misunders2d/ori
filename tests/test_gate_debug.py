import os
from app.core.whitelist import get_whitelist, get_blacklist

def test_gate_debug():
    print(f"\nALLOWED_USER_IDS: {os.environ.get('ALLOWED_USER_IDS')}")
    print(f"ADMIN_USER_IDS: {os.environ.get('ADMIN_USER_IDS')}")
    print(f"Whitelist cache: {get_whitelist()}")
    print(f"Blacklist cache: {get_blacklist()}")
    assert True
