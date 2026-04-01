import subprocess
import os

def test_git_rev_parse():
    # Verify we can run git rev-parse HEAD
    res = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    assert res.returncode == 0
    assert len(res.stdout.strip()) == 40

def test_git_diff_index():
    # Verify we can run git diff-index
    res = subprocess.run(["git", "diff-index", "--quiet", "HEAD", "--"], capture_output=True, text=True)
    # Should return 0 or 1, not error
    assert res.returncode in [0, 1]

def test_git_rev_parse_origin():
    # Verify if origin/master exists
    # Use master as it's the branch in .git/config
    res = subprocess.run(["git", "rev-parse", "origin/master"], capture_output=True, text=True)
    # If this fails, it means we don't have a remote tracking ref for master
    # which is possible if it's a shallow clone or no fetch was ever done
    assert res.returncode == 0
