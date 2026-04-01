import os
def test_gate_fail():
    allowed = os.environ.get('ALLOWED_USER_IDS')
    raise AssertionError(f"ALLOWED: {allowed}")
