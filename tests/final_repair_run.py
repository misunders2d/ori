
import os
import sys
# Add /code to sys.path to ensure we can import app
sys.path.append("/code")
from app.tools.system import repair_data_permissions

def test_execute_repair():
    print("\n--- LIVE REPAIR EXECUTION ---")
    result = repair_data_permissions()
    print(f"STATUS: {result.get('status')}")
    print(f"MESSAGE: {result.get('message')}")
    assert result["status"] == "success"
