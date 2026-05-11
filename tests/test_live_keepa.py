"""Live Keepa API smoke check — opt-in only.

This test makes a real outbound HTTP call to keepa.com. The shared
`conftest.py` fixture `restrict_live_http_calls` autouse-blocks httpx
across the suite to keep tests hermetic, so any default pytest run would
turn this into a guaranteed failure even when `KEEPA_API_KEY` is set.

Gate behaviour:
- `@pytest.mark.infra`: registered in `pyproject.toml`. Run via
  `pytest -m infra` to include; default runs deselect.
- `ORI_RUN_LIVE_KEEPA=1`: secondary opt-in. Without it the test skips
  even when the `infra` marker is selected — required because the
  conftest still mocks httpx unless live HTTP is explicitly allowed.

To exercise locally:

    ORI_RUN_LIVE_KEEPA=1 uv run python -m pytest tests/test_live_keepa.py -m infra -s
"""

import os

import pytest


@pytest.mark.infra
@pytest.mark.asyncio
async def test_keepa(monkeypatch):
    if os.environ.get("ORI_RUN_LIVE_KEEPA", "") != "1":
        pytest.skip(
            "Live Keepa test is opt-in. Set ORI_RUN_LIVE_KEEPA=1 and run with "
            "`-m infra` to exercise the real API."
        )
    if not os.environ.get("KEEPA_API_KEY"):
        pytest.skip("KEEPA_API_KEY not configured.")

    # The shared conftest autouse-blocks httpx for hermeticity. Restore real
    # httpx for this specific call so the live API request can fly. Done
    # via monkeypatch so the unblock is scoped to this test only.
    import importlib
    real_httpx = importlib.import_module("httpx")
    monkeypatch.setattr("httpx.AsyncClient", real_httpx.AsyncClient)
    monkeypatch.setattr("httpx.Client", real_httpx.Client)

    from app.tools.keepa_api import keepa_check_tokens

    res = await keepa_check_tokens()
    print("KEEPA TOKENS:", res)
    assert res["status"] == "success", str(res)
