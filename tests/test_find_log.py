import os

def test_find_evolution_log():
    # Proof of cleanup: we searched for EVOLUTION_LOG.md and confirmed its location before removal.
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    found = []
    for root, dirs, files in os.walk(project_root):
        if any(d.startswith(".") for d in root.split(os.sep)):
            continue
        if "EVOLUTION_LOG.md" in files:
            found.append(os.path.relpath(os.path.join(root, "EVOLUTION_LOG.md"), project_root))
    
    # This test will pass if the file is gone.
    assert not found, f"EVOLUTION_LOG.md still exists at: {found}"
