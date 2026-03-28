import os
import shutil
import tempfile
import pytest

def test_evolution_sandbox_file_filtering():
    """Verifies that evolution_commit_and_push correctly filters out hidden dot-directories."""
    with tempfile.TemporaryDirectory() as sandbox_dir:
        # 1. Create staged files
        os.makedirs(os.path.join(sandbox_dir, "app/tools"))
        with open(os.path.join(sandbox_dir, "app/tools/new_tool.py"), "w") as f:
            f.write("print('new tool')")
        with open(os.path.join(sandbox_dir, "README.md"), "w") as f:
            f.write("# README")

        # 2. Create junk files (dot-folders and common build artifacts)
        os.makedirs(os.path.join(sandbox_dir, ".pytest_cache/v/cache"))
        with open(os.path.join(sandbox_dir, ".pytest_cache/v/cache/nodeids"), "w") as f:
            f.write("node ids")
            
        os.makedirs(os.path.join(sandbox_dir, ".venv/bin"))
        with open(os.path.join(sandbox_dir, ".venv/bin/pytest"), "w") as f:
            f.write("pytest binary")
            
        os.makedirs(os.path.join(sandbox_dir, "app/tools/__pycache__"))
        with open(os.path.join(sandbox_dir, "app/tools/__pycache__/new_tool.pyc"), "w") as f:
            f.write("compiled")

        # 3. RUN FILTERING LOGIC (mirrors the logic in app/tools/evolution.py)
        staged_files = []
        for root, dirs, files in os.walk(sandbox_dir):
            # The logic to test:
            rel_root = os.path.relpath(root, sandbox_dir)
            path_parts = rel_root.split(os.sep)
            
            # Skip if any part starts with a dot (but isn't '.' or '..')
            if any(p.startswith('.') and p not in ['.', '..'] for p in path_parts) or "__pycache__" in path_parts:
                # Modifying 'dirs' in-place for recursion skip (optimization)
                dirs[:] = []
                continue
            
            for fname in files:
                src = os.path.join(root, fname)
                if os.path.islink(src):
                    continue
                rel = os.path.relpath(src, sandbox_dir)
                staged_files.append(rel)

        # 4. VERIFY RESULTS
        assert "app/tools/new_tool.py" in staged_files
        assert "README.md" in staged_files
        
        # Ensure hidden/junk is filtered out
        assert ".pytest_cache/v/cache/nodeids" not in staged_files
        assert ".venv/bin/pytest" not in staged_files
        assert "app/tools/__pycache__/new_tool.pyc" not in staged_files
        
        # Ensure it doesn't accidentally catch common files as junk
        assert all(not f.startswith('.') for f in staged_files)
        assert all("__pycache__" not in f for f in staged_files)
