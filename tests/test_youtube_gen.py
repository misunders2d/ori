"""Tests for the multimodal YouTube summarization tool."""

import pytest
from app.tools.youtube import youtube_summary

@pytest.mark.asyncio
async def test_youtube_summary_mocked(monkeypatch):
    """Test that the youtube_summary tool calls the Gemini client correctly."""
    
    class MockPart:
        @classmethod
        def from_uri(cls, file_uri, mime_type):
            return {"file_uri": file_uri, "mime_type": mime_type}
        @classmethod
        def from_text(cls, text):
            return {"text": text}

    class MockCandidate:
        def __init__(self):
            self.finish_reason = "STOP"
            class MockContent:
                def __init__(self):
                    class MockTextPart:
                        def __init__(self):
                            self.text = "This is a test summary."
                    self.parts = [MockTextPart()]
            self.content = MockContent()

    class MockResponse:
        def __init__(self):
            self.candidates = [MockCandidate()]

    class MockModels:
        async def generate_content(self, model, contents, **kwargs):
            return MockResponse()

    class MockClient:
        def __init__(self, **kwargs):
            class MockAio:
                def __init__(self):
                    self.models = MockModels()
            self.aio = MockAio()

    # Patch the genai.Client and types
    import google.genai
    monkeypatch.setattr("google.genai.Client", MockClient)
    monkeypatch.setattr("google.genai.types.Part", MockPart)

    # Patch model resolver
    monkeypatch.setenv("GOOGLE_API_KEY", "fake_key")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

    result = await youtube_summary(url="https://youtube.com/watch?v=123", query="summary")
    
    assert result["status"] == "success"
    assert "This is a test summary." in result["message"]
