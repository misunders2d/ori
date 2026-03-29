import os

def test_readme_contains_joke():
    # In the sandbox, README.md is in the current working directory
    readme_path = "README.md"
    assert os.path.exists(readme_path), "README.md does not exist in sandbox"
    with open(readme_path, "r") as f:
        content = f.read()
    assert "### 😄 Just for fun" in content
    assert "Why did the programmer quit his job?" in content
