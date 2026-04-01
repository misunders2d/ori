import os
import json

async def check_admins():
    # Only reveal the IDs, no other secrets
    admins = os.environ.get("ADMIN_USER_IDS", "NOT_SET")
    allowed = os.environ.get("ALLOWED_USER_IDS", "NOT_SET")
    
    data = {
        "ADMIN_USER_IDS": admins,
        "ALLOWED_USER_IDS": allowed
    }
    
    with open("data/admin_debug.json", "w") as f:
        json.dump(data, f, indent=2)
        
    return f"Admin debug data written. {len(admins.split(','))} admin(s) found."
