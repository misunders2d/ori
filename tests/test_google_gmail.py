"""Tests for Gmail read-only tools — helpers and tool surface."""

from app.tools.google_oauth.device_flow import SCOPES


def test_gmail_readonly_scope_present():
    assert "https://www.googleapis.com/auth/gmail.readonly" in SCOPES
