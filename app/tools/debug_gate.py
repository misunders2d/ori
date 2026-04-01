from app.core.whitelist import get_whitelist, get_blacklist
import json
import os

def check_gate():
    data = {
        "whitelist": get_whitelist(),
        "blacklist": get_blacklist()
    }
    with open("data/gate_debug.json", "w") as f:
        json.dump(data, f, indent=2)
    return f"Debug data written to data/gate_debug.json. Whitelist size: {len(data['whitelist'])}"

if __name__ == "__main__":
    print(check_gate())
