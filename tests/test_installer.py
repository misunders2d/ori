import os
import shutil
import subprocess
import tempfile
import pytest

def test_dna_detachment_logic():
    """Verifies the core 'detachment' logic used in install scripts."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Simulate a 'clone' by initializing a repo with history
        repo_dir = os.path.join(tmpdir, "ori-organism")
        os.makedirs(repo_dir)
        
        subprocess.run(["git", "init", "-b", "master"], cwd=repo_dir, check=True, capture_output=True)
        with open(os.path.join(repo_dir, "README.md"), "w") as f:
            f.write("# Original Ori")
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial upstream commit"], cwd=repo_dir, check=True, capture_output=True)
        
        # Verify it has history
        result = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
        assert result.stdout.strip() == "1"
        assert os.path.exists(os.path.join(repo_dir, ".git"))

        # 2. SEVER DNA (The Detachment)
        shutil.rmtree(os.path.join(repo_dir, ".git"))
        assert not os.path.exists(os.path.join(repo_dir, ".git"))

        # 3. RE-BIRTH (Fresh start)
        subprocess.run(["git", "init", "-b", "master"], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial birth of Ori Organism"], cwd=repo_dir, check=True, capture_output=True)

        # Verify history is reset
        result = subprocess.run(["git", "log", "--oneline"], cwd=repo_dir, capture_output=True, text=True)
        assert "Initial birth of Ori Organism" in result.stdout
        assert "Initial upstream commit" not in result.stdout
        
        # Verify it's a standalone repo
        result = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=repo_dir, capture_output=True, text=True)
        assert result.stdout.strip() == "1"
