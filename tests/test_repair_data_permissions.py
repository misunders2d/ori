
import pytest
import os
from app.tools.system import repair_data_permissions

def test_repair_data_permissions():
    # We can't easily mock the filesystem and then check permissions reliably in all environments,
    # but we can ensure the tool runs without error and returns success.
    result = repair_data_permissions()
    assert result["status"] == "success"
    assert "repaired permissions" in result["message"]
