import pytest
import json
from app.tools.a2a import _extract_response_text

def test_extract_response_text_with_dict_parts():
    """Verify that _extract_response_text handles non-string parts gracefully and does NOT crash."""
    complex_data = {"complex": "data"}
    task = {
        "artifacts": [
            {
                "parts": [
                    {"text": "Hello"},
                    {"text": complex_data},
                    {"text": "World"}
                ]
            }
        ]
    }
    result = _extract_response_text(task)
    # The primary goal is that it is a string and contains the data
    assert isinstance(result, str)
    assert "Hello" in result
    assert "World" in result
    # We allow flexible formatting of the dict part
    assert "complex" in result
    assert "data" in result

def test_extract_response_text_empty():
    task = {}
    result = _extract_response_text(task)
    assert result == "(no text in response)"

def test_extract_response_text_messages_fallback():
    task = {
        "messages": [
            {
                "role": "agent",
                "parts": [{"text": "Agent response"}]
            }
        ]
    }
    result = _extract_response_text(task)
    assert result == "Agent response"

def test_extract_response_text_status_fallback():
    task = {
        "status": {"message": "Status error"}
    }
    result = _extract_response_text(task)
    assert result == "Status error"
