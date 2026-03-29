import importlib.metadata

def test_list_pkgs():
    dists = [d.metadata['Name'].lower() for d in importlib.metadata.distributions()]
    print("\n--- INSTALLED PACKAGES ---")
    print(", ".join(dists))
    assert any("google" in d for d in dists), "No google packages found"
