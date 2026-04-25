import os
import pytest
from app.runtime.channel_logger import log_message, get_logs

def test_channel_logging():
    cid = "tg_-100channel"
    log_message(cid, "u123", "Tester", "Security alert")
    
    logs = get_logs(cid, hours=1)
    assert len(logs) > 0
    assert logs[0]['text'] == "Security alert"
    assert logs[0]['display_name'] == "Tester"

def test_no_logs():
    cid = "tg_-empty_channel"
    logs = get_logs(cid, hours=1)
    assert len(logs) == 0
