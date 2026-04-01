import os
def test_env_raw():
    allowed = os.environ.get("ALLOWED_USER_IDS")
    admins = os.environ.get("ADMIN_USER_IDS")
    # Using print to see in logs
    print(f"\nRAW_ALLOWED: {repr(allowed)}")
    print(f"RAW_ADMINS: {repr(admins)}")
    # We force a failure to see the output in the tool response
    raise AssertionError(f"ALLOWED: {allowed} | ADMINS: {admins}")
