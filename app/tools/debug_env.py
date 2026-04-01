import os
import json

async def debug_whitelist_env() -> str:
    """Returns the values of whitelist-related environment variables."""
    allowed = os.environ.get("ALLOWED_USER_IDS", "NOT_SET")
    admins = os.environ.get("ADMIN_USER_IDS", "NOT_SET")
    
    # Redact most of the string for security, but show enough to identify
    def redact(s):
        if s == "NOT_SET": return s
        if len(s) < 10: return s
        return s[:5] + "..." + s[-5:]
        
    return f"ALLOWED_USER_IDS: {redact(allowed)} | ADMIN_USER_IDS: {redact(admins)}"
