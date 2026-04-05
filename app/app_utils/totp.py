"""
TOTP (RFC 6238) implementation using only Python stdlib.

No external dependencies — aligns with the Native Tools First mandate.
Generates and verifies 6-digit, 30-second TOTP codes compatible with
Google Authenticator, Authy, and other standard authenticator apps.
"""

import base64
import hashlib
import hmac
import struct
import time


def _decode_secret(secret: str) -> bytes:
    """Decode a base32-encoded TOTP secret, tolerating missing padding."""
    secret = secret.strip().upper()
    # Add padding if needed
    padding = 8 - (len(secret) % 8)
    if padding != 8:
        secret += "=" * padding
    return base64.b32decode(secret)


def _generate_code(secret: str, time_step: int) -> str:
    """Generate a 6-digit TOTP code for a given time step."""
    key = _decode_secret(secret)
    msg = struct.pack(">Q", time_step)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


def verify_totp(secret: str, code: str, window: int = 1) -> bool:
    """
    Verify a TOTP code against a secret.

    Checks the current time step and +/- `window` steps to account for
    clock drift (default: +/- 30 seconds).
    """
    code = code.strip()
    if len(code) != 6 or not code.isdigit():
        return False

    current_step = int(time.time()) // 30
    for offset in range(-window, window + 1):
        if hmac.compare_digest(_generate_code(secret, current_step + offset), code):
            return True
    return False
