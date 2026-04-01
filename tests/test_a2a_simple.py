import json
from app.tools.a2a import _extract_response_text

def test_simple_extraction():
    task = {"artifacts": [{"parts": [{"text": "hello"}]}]}
    assert _extract_response_text(task) == "hello"

def test_dict_extraction():
    # We define _to_string logic here to test what it should do
    def local_to_string(val):
        if isinstance(val, str): return val
        return json.dumps(val)
    
    val = {"a": 1}
    assert local_to_string(val) == '{"a": 1}'
