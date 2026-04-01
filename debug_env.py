import os
import json
from app.core.whitelist import get_whitelist, get_blacklist

data = {
    "ALLOWED_USER_IDS": os.environ.get("ALLOWED_USER_IDS"),
    "ADMIN_USER_IDS": os.environ.get("ADMIN_USER_IDS"),
    "whitelist": get_whitelist(),
    "blacklist": get_blacklist()
}
with open("data/diagnostic_results.json", "w") as f:
    json.dump(data, f, indent=2)
print("Diagnostic results written to data/diagnostic_results.json")
