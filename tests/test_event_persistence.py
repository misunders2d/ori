import pytest
import time
import uuid
from unittest.mock import MagicMock, AsyncMock, patch
from google.genai import types

@pytest.mark.asyncio
async def test_process_message_for_context_event_creation():
    """Verify that process_message_for_context creates a valid Event with author and id."""
    
    # Import the function
    from app.core.agent_executor import process_message_for_context
    
    # Mock runner and session service
    runner = MagicMock()
    runner.app_name = "test_app"
    runner.session_service = AsyncMock()
    
    # Mock session
    mock_session = MagicMock()
    runner.session_service.get_session.return_value = mock_session
    
    user_id = "test_user"
    session_id = "test_session"
    message = "Hello context"

    # We need to mock 'Event' specifically where it's used inside the function
    with patch("app.core.agent_executor.Event") as mock_event_class, \
         patch("app.core.agent_executor.uuid.uuid4", return_value="fixed-uuid"), \
         patch("app.core.agent_executor.time.time", return_value=123456789.0):
        
        await process_message_for_context(runner, user_id, session_id, message)

    # Verify Event was instantiated with correct fields (id, author, timestamp, content)
    mock_event_class.assert_called_once()
    kwargs = mock_event_class.call_args.kwargs
    
    assert kwargs["author"] == user_id
    assert kwargs["id"] == "fixed-uuid"
    assert kwargs["timestamp"] == 123456789.0
    assert isinstance(kwargs["content"], types.Content)
    assert kwargs["content"].parts[0].text == message
