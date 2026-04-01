import os
import json
from app.core.whitelist import get_whitelist, get_blacklist

def dump_gate():
    data = {
        "whitelist": get_whitelist(),
        "blacklist": get_blacklist(),
        "ALLOWED_USER_IDS": os.environ.get("ALLOWED_USER_IDS"),
        "ADMIN_USER_IDS": os.environ.get("ADMIN_USER_IDS")
    }
    with open("data/gate_dump.json", "w") as f:
        json.dump(data, f, indent=2)
    return "Done"

if __name__ == "__main__":
    dump_gate()
