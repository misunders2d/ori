import os
from app.core.whitelist import get_whitelist

def test_leak():
    wl = get_whitelist()
    # Check for the wife's ID specifically if it appears in the whitelist
    raise AssertionError(f"WHITELIST_CONTAINS_185625742: {'tg_185625742' in wl} | FULL_LIST: {wl}")
