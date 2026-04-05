"""Tests for the TOTP (RFC 6238) implementation."""

import time

from app.app_utils.totp import verify_totp, _generate_code, _decode_secret


def test_decode_secret_with_padding():
    """Base32 secrets with missing padding should decode correctly."""
    secret = "JBSWY3DPEHPK3PXP"
    result = _decode_secret(secret)
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_decode_secret_already_padded():
    """Base32 secrets already padded should decode without error."""
    secret = "JBSWY3DPEHPK3PXP"
    padded = secret + "=" * (8 - len(secret) % 8) if len(secret) % 8 else secret
    result = _decode_secret(padded)
    assert isinstance(result, bytes)


def test_generate_code_format():
    """Generated codes should be exactly 6 digits."""
    secret = "JBSWY3DPEHPK3PXP"
    step = int(time.time()) // 30
    code = _generate_code(secret, step)
    assert len(code) == 6
    assert code.isdigit()


def test_generate_code_deterministic():
    """Same secret + time step should always produce the same code."""
    secret = "JBSWY3DPEHPK3PXP"
    step = 12345678
    code1 = _generate_code(secret, step)
    code2 = _generate_code(secret, step)
    assert code1 == code2


def test_generate_code_different_steps():
    """Different time steps should (almost always) produce different codes."""
    secret = "JBSWY3DPEHPK3PXP"
    code1 = _generate_code(secret, 1000000)
    code2 = _generate_code(secret, 1000001)
    # Technically could collide, but astronomically unlikely
    assert code1 != code2


def test_verify_current_code():
    """A code generated for the current time step should verify."""
    secret = "JBSWY3DPEHPK3PXP"
    step = int(time.time()) // 30
    code = _generate_code(secret, step)
    assert verify_totp(secret, code) is True


def test_verify_adjacent_step():
    """Codes from adjacent time steps should verify within window=1."""
    secret = "JBSWY3DPEHPK3PXP"
    step = int(time.time()) // 30
    code_prev = _generate_code(secret, step - 1)
    code_next = _generate_code(secret, step + 1)
    assert verify_totp(secret, code_prev, window=1) is True
    assert verify_totp(secret, code_next, window=1) is True


def test_verify_outside_window():
    """Codes far outside the window should fail."""
    secret = "JBSWY3DPEHPK3PXP"
    step = int(time.time()) // 30
    code_old = _generate_code(secret, step - 10)
    assert verify_totp(secret, code_old, window=1) is False


def test_verify_wrong_code():
    """A code generated for a different secret should fail."""
    assert verify_totp("AAAAAAAAAAAAAAAA", _generate_code("JBSWY3DPEHPK3PXP", 0)) is False


def test_verify_rejects_non_digits():
    """Non-digit input should be rejected."""
    secret = "JBSWY3DPEHPK3PXP"
    assert verify_totp(secret, "abcdef") is False
    assert verify_totp(secret, "12345a") is False


def test_verify_rejects_wrong_length():
    """Codes that aren't exactly 6 digits should be rejected."""
    secret = "JBSWY3DPEHPK3PXP"
    assert verify_totp(secret, "12345") is False
    assert verify_totp(secret, "1234567") is False
    assert verify_totp(secret, "") is False


def test_verify_strips_whitespace():
    """Leading/trailing whitespace should be tolerated."""
    secret = "JBSWY3DPEHPK3PXP"
    step = int(time.time()) // 30
    code = _generate_code(secret, step)
    assert verify_totp(secret, f"  {code}  ") is True
