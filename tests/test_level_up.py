import os
import re

def test_version_bump():
    with open('pyproject.toml', 'r') as f:
        content = f.read()
    match = re.search(r'version\s*=\s*"(.*?)"', content)
    assert match is not None
    assert match.group(1) == '0.7.0'

def test_changelog_exists():
    assert os.path.exists('CHANGELOG.md')
    with open('CHANGELOG.md', 'r') as f:
        content = f.read()
    assert '## [0.7.0] - 2024-03-29' in content
    # Ensure no future dates left
    # Look for '2026' followed by '-' and month (to avoid false positives with other numbers)
    assert not re.search(r'2026-\d{2}-\d{2}', content)

def test_readme_updates():
    with open('README.md', 'r') as f:
        content = f.read()
    assert '## 🌐 The Ori-Net Bridge [UNLOCKED]' in content
    assert '## ⚡ Feature Showcase (Ability Tree)' in content
    assert 'Tactical Silence' in content
