import pytest
from app.tools.keepa_api import keepa_check_tokens

@pytest.mark.asyncio
async def test_keepa():
    res = await keepa_check_tokens()
    print("KEEPA TOKENS:", res)
    assert False, str(res) # Force failure to see output
