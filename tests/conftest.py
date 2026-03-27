import pytest


@pytest.fixture(autouse=True)
def restrict_live_http_calls(monkeypatch):
    """
    SECURITY BOUNDARY:
    Globally patches all common HTTP clients during pytest execution
    to prevent the DeveloperAgent from inadvertently hitting live APIs,
    sending Telegram/Slack messages, or leaking data during sandbox
    regression testing.

    Blocked libraries: httpx (async + sync), requests.
    Tests that need HTTP must explicitly mock the specific calls they need.
    """
    _BLOCK_MSG = (
        "Sandbox Security Guardrail: Live HTTP {method} requests are blocked during "
        "agent test simulations. You must explicitly mock the HTTP client in your "
        "test logic to verify integration flows."
    )

    # --- httpx.AsyncClient (async) ---
    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def get(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="GET"))

        async def post(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="POST"))

        async def put(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="PUT"))

        async def delete(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="DELETE"))

        async def patch(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="PATCH"))

    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)

    # --- httpx.Client (sync) ---
    class MockSyncClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def get(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="GET"))

        def post(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="POST"))

        def put(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="PUT"))

        def delete(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="DELETE"))

        def patch(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="PATCH"))

    monkeypatch.setattr("httpx.Client", MockSyncClient)

    # --- requests library ---
    def _blocked_request(method):
        def _raise(*args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method=method))
        return _raise

    try:
        import requests
        monkeypatch.setattr("requests.get", _blocked_request("GET"))
        monkeypatch.setattr("requests.post", _blocked_request("POST"))
        monkeypatch.setattr("requests.put", _blocked_request("PUT"))
        monkeypatch.setattr("requests.delete", _blocked_request("DELETE"))
        monkeypatch.setattr("requests.patch", _blocked_request("PATCH"))
        monkeypatch.setattr("requests.request", _blocked_request("REQUEST"))
    except ImportError:
        pass  # requests not installed, nothing to patch
