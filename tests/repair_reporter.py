
import pytest
import os
from app.tools.system import repair_data_permissions
from app.tools.evolution import remember_info

def test_run_repair_and_report():
    result = repair_data_permissions()
    # Attempt to report via remember_info from WITHIN the test
    try:
        remember_info(
            category="background_tasks",
            content=f"Sandbox repair result: {result.get('status')}: {result.get('message')}",
            importance=4
        )
    except Exception as e:
        print(f"Failed to report: {e}")
    assert result["status"] == "success"
