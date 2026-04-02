
import pytest
import os
from app.tools.diagnostics import repair_data_permissions

def test_repair_data_permissions():
    result = repair_data_permissions()
    assert result["status"] == "success"
    assert "Repaired" in result["message"]
