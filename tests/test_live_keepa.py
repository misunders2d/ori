import os

import pytest

from app.tools.keepa_api import keepa_check_tokens


@pytest.mark.infra
@pytest.mark.asyncio
async def test_keepa():
    if not os.environ.get("KEEPA_API_KEY"):
        pytest.skip("KEEPA_API_KEY not configured")

    res = await keepa_check_tokens()
    print("KEEPA TOKENS:", res)
    assert res["status"] == "success", str(res)
