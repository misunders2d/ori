import pytest

@pytest.fixture(autouse=True)
def restrict_live_http_calls(monkeypatch):
    """
    SECURITY BOUNDARY: 
    Globally patches httpx.AsyncClient during pytest execution 
    to prevent the DeveloperAgent from inadvertently spamming 
    live Telegram/Slack production channels while conducting 
    sandbox regression testing.
    """
    class MockAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def get(self, *args, **kwargs):
            raise RuntimeError(
                "Sandbox Security Guardrail: Live HTTP GET requests are blocked during agent test simulations. "
                "You must heavily mock `httpx.AsyncClient` or `requests.get` explicitly in your test logic to verify integration flows."
            )

        async def post(self, *args, **kwargs):
            raise RuntimeError(
                "Sandbox Security Guardrail: Live HTTP POST requests are blocked during agent test simulations. "
                "You must mock out your messaging or payload actions to ensure testing is completely isolated."
            )

    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)
