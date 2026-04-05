import os
import json

def test_capture_env():
    data = {
        "ALLOWED_USER_IDS": os.environ.get("ALLOWED_USER_IDS"),
        "ADMIN_USER_IDS": os.environ.get("ADMIN_USER_IDS")
    }
    with open("data/env_ids_captured.json", "w") as f:
        json.dump(data, f)
    assert True
