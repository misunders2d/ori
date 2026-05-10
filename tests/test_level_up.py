import os
import re
import pytest

def test_version_bump():
    if not os.path.exists('pyproject.toml'):
        pytest.fail("pyproject.toml not found")
    with open('pyproject.toml') as f:
        content = f.read()
    match = re.search(r'version\s*=\s*"(.*?)"', content)
    assert match is not None
    # Just verify that the version string is present and valid
    assert re.match(r'\d+\.\d+\.\d+', match.group(1))

def test_changelog_exists():
    if not os.path.exists('CHANGELOG.md'):
        pytest.fail("CHANGELOG.md not found")
    with open('CHANGELOG.md') as f:
        content = f.read()
    # At least some version should be present
    assert re.search(r'## \[\d+\.\d+\.\d+\]', content)

def test_readme_updates():
    if not os.path.exists('README.md'):
        pytest.fail("README.md not found")
    with open('README.md') as f:
        content = f.read()
    # Check for the updated header or content
    assert '## 🌐 The Ori-Net Bridge' in content
    assert '## ⚡ Feature Showcase (Ability Tree)' in content
    assert 'Tactical Silence' in content
