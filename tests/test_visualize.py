import os
import pytest
from unittest.mock import patch

from app.tools.visualize import generate_chart, _get_module


def test_generate_chart_empty_code():
    result = generate_chart(code="", filename="test.png")
    assert result["status"] == "error"
    assert "No code" in result["message"]


def test_generate_chart_none_code():
    result = generate_chart(code=None, filename="test.png")
    assert result["status"] == "error"


def test_generate_chart_matplotlib():
    code = '''
import matplotlib.pyplot as plt
fig, ax = plt.subplots()
ax.plot([1, 2, 3], [4, 5, 6])
ax.set_title("Test")
plt.savefig(OUTPUT_PATH)
'''
    result = generate_chart(code=code, filename="test_mpl.png")
    assert result["status"] == "success"
    assert result["size_bytes"] > 0
    assert os.path.exists(result["file_path"])
    os.remove(result["file_path"])


def test_generate_chart_auto_save():
    """matplotlib figures auto-save if code doesn't call savefig."""
    code = '''
import matplotlib.pyplot as plt
plt.plot([1, 2, 3], [10, 20, 30])
plt.title("Auto-save test")
'''
    result = generate_chart(code=code, filename="test_auto.png")
    assert result["status"] == "success"
    assert os.path.exists(result["file_path"])
    os.remove(result["file_path"])


def test_generate_chart_bad_code():
    result = generate_chart(code="raise ValueError('intentional')", filename="bad.png")
    assert result["status"] == "error"
    assert "intentional" in result["message"]


def test_generate_chart_blocked_import():
    code = '''
import subprocess
subprocess.run(["ls"])
'''
    result = generate_chart(code=code, filename="hack.png")
    assert result["status"] == "error"
    assert "not allowed" in result["message"]


def test_generate_chart_no_file_saved():
    code = '''
x = 1 + 1
'''
    result = generate_chart(code=code, filename="nothing.png")
    assert result["status"] == "error"
    assert "no file was saved" in result["message"].lower()


def test_lazy_module_loading():
    """Verify matplotlib.pyplot loads lazily without error."""
    mod = _get_module("matplotlib.pyplot")
    assert mod is not None
    assert hasattr(mod, "savefig")


def test_disallowed_module():
    mod = _get_module("subprocess")
    assert mod is None
