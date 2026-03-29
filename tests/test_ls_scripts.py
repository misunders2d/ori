import os
import pytest
def test_list_scripts():
    path = "scripts"
    if not os.path.exists(path):
        pytest.skip("scripts directory not found")
    files = os.listdir(path)
    assert len(files) > 0, "Scripts directory is empty"
