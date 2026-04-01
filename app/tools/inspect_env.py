import os
import json

def inspect():
    data = {
        "ALLOWED_USER_IDS": os.environ.get("ALLOWED_USER_IDS"),
        "ADMIN_USER_IDS": os.environ.get("ADMIN_USER_IDS"),
    }
    with open("data/env_inspect.json", "w") as f:
        json.dump(data, f, indent=2)
    return "Done"

if __name__ == "__main__":
    inspect()
