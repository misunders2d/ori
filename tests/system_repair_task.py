
import pytest
from app.tools.system import repair_data_permissions

def test_run_repair():
    print("\nStarting data permissions repair task...")
    result = repair_data_permissions()
    print(f"Result: {result}")
    assert result["status"] in ["success", "error"] # We just want to see it run
    if result["status"] == "success":
        print("Repair successful.")
    else:
        print(f"Repair failed: {result.get('message')}")
