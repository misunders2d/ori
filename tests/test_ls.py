import os
def test_list_files():
    files = os.listdir(".")
    print(f"\nDEBUG: Files in '.' are: {files}")
    assert "pyproject.toml" in files
