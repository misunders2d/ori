import os
from unittest.mock import MagicMock
from google.adk.a2a.utils.agent_to_a2a import to_a2a

mock_agent = MagicMock()
app = to_a2a(mock_agent)
print(f"Routes list: {app.routes}")
for r in app.routes:
    print(f"Path: {getattr(r, 'path', 'N/A')}")
