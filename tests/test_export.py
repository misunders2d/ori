import os
import pytest

from app.tools.export_file import generate_file


def test_generate_file_empty_code():
    result = generate_file(code="", filename="test.csv")
    assert result["status"] == "error"
    assert "No code" in result["message"]


def test_generate_file_no_filename():
    result = generate_file(code="x = 1", filename="")
    assert result["status"] == "error"
    assert "Filename is required" in result["message"]


def test_generate_file_csv():
    code = '''
import csv
with open(OUTPUT_PATH, 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(["date", "sales", "price"])
    writer.writerow(["2026-03-01", 120, 36.97])
    writer.writerow(["2026-03-02", 135, 36.97])
'''
    result = generate_file(code=code, filename="test_output.csv")
    assert result["status"] == "success"
    assert result["size_bytes"] > 0
    assert os.path.exists(result["file_path"])
    with open(result["file_path"]) as f:
        content = f.read()
    assert "2026-03-01" in content
    assert "36.97" in content
    os.remove(result["file_path"])


def test_generate_file_markdown():
    code = '''
with open(OUTPUT_PATH, 'w') as f:
    f.write("# Report\\n\\n| ASIN | Sales |\\n|---|---|\\n| B00X | 120 |\\n")
'''
    result = generate_file(code=code, filename="report.md")
    assert result["status"] == "success"
    assert os.path.exists(result["file_path"])
    os.remove(result["file_path"])


def test_generate_file_pandas_csv():
    code = '''
import pandas as pd
df = pd.DataFrame({"asin": ["B001", "B002"], "sales": [100, 200]})
df.to_csv(OUTPUT_PATH, index=False)
'''
    result = generate_file(code=code, filename="pandas_test.csv")
    assert result["status"] == "success"
    assert os.path.exists(result["file_path"])
    os.remove(result["file_path"])


def test_generate_file_bad_code():
    result = generate_file(code="1/0", filename="fail.csv")
    assert result["status"] == "error"
    assert "division by zero" in result["message"]


def test_generate_file_blocked_import():
    code = '''
import os
os.listdir("/")
'''
    result = generate_file(code=code, filename="hack.csv")
    assert result["status"] == "error"
    assert "not allowed" in result["message"]


def test_generate_file_no_output():
    result = generate_file(code="x = 42", filename="ghost.csv")
    assert result["status"] == "error"
    assert "no file was saved" in result["message"].lower()
