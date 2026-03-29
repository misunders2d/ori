import os
import sys
# Mock root_agent since it might import too much
from unittest.mock import MagicMock

try:
    from google.adk.a2a.utils.agent_to_a2a import to_a2a
    mock_agent = MagicMock()
    app = to_a2a(mock_agent)
    print(f"App type: {type(app)}")
    print(f"App attributes: {dir(app)}")
except Exception as e:
    print(f"Error: {e}")
