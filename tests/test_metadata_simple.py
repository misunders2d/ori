from datetime import datetime


def format_metadata_test(text, timestamp, platform):
    ts_str = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    header = f"[Metadata: {ts_str} | Platform: {platform}]"
    return f"{header}\n{text}"

def test_metadata_formatting():
    ts = datetime(2026, 4, 2, 16, 30)
    text = "Hello"
    result = format_metadata_test(text, ts, "telegram")
    assert "[Metadata: 2026-04-02 16:30:00 UTC | Platform: telegram]" in result
    assert "Hello" in result
