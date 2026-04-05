import pytest
from datetime import datetime
from app.core.agent_executor import _inject_metadata_header

class MockMessage:
    def __init__(self, text, timestamp, platform):
        self.text = text
        self.timestamp = timestamp
        self.platform = platform

def test_metadata_header_injection_utc():
    # Test UTC (default)
    ts = datetime(2026, 4, 2, 16, 30)
    msg = MockMessage(text="Hello", timestamp=ts, platform="telegram")
    state = {"user_preferences": ""}
    
    result = _inject_metadata_header(msg.text, msg.timestamp, msg.platform, state)
    assert "[Metadata: 2026-04-02 16:30:00 UTC | Platform: telegram]" in result
    assert "Hello" in result

def test_metadata_header_injection_timezone():
    # Test with custom timezone in preferences
    ts = datetime(2026, 4, 2, 16, 30) # 16:30 UTC
    msg = MockMessage(text="Hello", timestamp=ts, platform="telegram")
    
    # Europe/Kyiv is UTC+3 in April (DST)
    state = {"user_preferences": "Timezone: Europe/Kyiv"}
    
    result = _inject_metadata_header(msg.text, msg.timestamp, msg.platform, state)
    # Kyiv is UTC+3 in April. 16:30 UTC -> 19:30 EEST
    assert "19:30:00" in result
    # Check for EEST or +03
    assert any(tz_id in result for tz_id in ["EEST", "GMT+3", "UTC+03:00", "+03"])
    assert "Platform: telegram" in result

def test_metadata_header_injection_invalid_timezone_fallback():
    ts = datetime(2026, 4, 2, 16, 30)
    msg = MockMessage(text="Hello", timestamp=ts, platform="telegram")
    state = {"user_preferences": "Timezone: Invalid/Zone"}
    
    result = _inject_metadata_header(msg.text, msg.timestamp, msg.platform, state)
    assert "UTC" in result
