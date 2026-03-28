import os

def test_find_evolution_log():
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    found = []
    for root, dirs, files in os.walk(project_root):
        # Skip common dot folders to be fast
        if any(d.startswith(".") for d in root.split(os.sep)):
            continue
        if "evolution_log.md" in files:
            found.append(os.path.relpath(os.path.join(root, "evolution_log.md"), project_root))
    
    if found:
        assert False, f"FOUND EVOLUTION LOG AT: {found}"
    else:
        assert True, "EVOLUTION LOG NOT FOUND"
