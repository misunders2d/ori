import os
def test_reveal_url():
    url = os.environ.get("A2A_BASE_URL", "NOT_SET")
    port = os.environ.get("A2A_PORT", "NOT_SET")
    raise ValueError(f"URL={url}, PORT={port}")
