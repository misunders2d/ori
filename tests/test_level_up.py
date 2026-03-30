import os
import re
import pytest

def test_version_bump():
    if not os.path.exists('pyproject.toml'):
        pytest.skip("pyproject.toml not found")
    with open('pyproject.toml', 'r') as f:
        content = f.read()
    match = re.search(r'version\s*=\s*"(.*?)"', content)
    assert match is not None
    assert match.group(1) == '0.8.0'

def test_changelog_exists():
    if not os.path.exists('CHANGELOG.md'):
        pytest.skip("CHANGELOG.md not found")
    with open('CHANGELOG.md', 'r') as f:
        content = f.read()
    assert '## [0.8.0] - 2024-03-29' in content
    # Ensure no future dates left
    # Look for '2026' followed by '-' and month (to avoid false positives with other numbers)
    assert not re.search(r'2026-\d{2}-\d{2}', content)

def test_readme_updates():
    if not os.path.exists('README.md'):
        pytest.skip("README.md not found")
    with open('README.md', 'r') as f:
        content = f.read()
    assert '## 🌐 The Ori-Net Bridge [UNLOCKED]' in content
    assert '## ⚡ Feature Showcase (Ability Tree)' in content
    assert 'Tactical Silence' in content
