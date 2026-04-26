import os


def test_dump_env():
    print(f"\nALLOWED_USER_IDS: {os.environ.get('ALLOWED_USER_IDS')}")
    print(f"ADMIN_USER_IDS: {os.environ.get('ADMIN_USER_IDS')}")
    assert True
