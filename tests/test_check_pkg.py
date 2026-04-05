import importlib.metadata

def test_check_pkg():
    # Robust check using importlib.metadata (standard in Python 3.10+)
    try:
        dist = importlib.metadata.distribution('google-adk')
        print(f"\n--- GOOGLE-ADK FOUND ---")
        print(f"Version: {dist.version}")
        assert dist.metadata['Name'].lower() == 'google-adk'
    except importlib.metadata.PackageNotFoundError:
        # Fallback for different distribution names if necessary
        dist = importlib.metadata.distribution('google-adk-python')
        assert dist.metadata['Name'].lower() == 'google-adk-python'
