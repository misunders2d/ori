import pytest
import os
from unittest.mock import AsyncMock, patch, MagicMock as SyncMagicMock

from app.tools.keepa_api import (
    keepa_fetch_product,
    keepa_extract_pricing,
    _price_from_csv,
    _history_from_csv,
)

# Mock token check response
_MOCK_TOKEN_RESP = SyncMagicMock()
_MOCK_TOKEN_RESP.status_code = 200
_MOCK_TOKEN_RESP.json.return_value = {"tokensLeft": 100, "refillRate": 5, "refillIn": 60000}
_MOCK_TOKEN_RESP.raise_for_status = SyncMagicMock()


def test_price_from_csv():
    # Active price
    csv = [100, 2000, 200, 1500]
    assert _price_from_csv(csv, 2) == 15.0

    # Inactive (-1)
    csv = [100, 2000, 200, -1]
    assert _price_from_csv(csv, 2) is None

    # Empty
    assert _price_from_csv([], 2) is None
    assert _price_from_csv(None, 2) is None


@pytest.mark.asyncio
async def test_fetch_product_no_key():
    with patch.dict(os.environ, {"KEEPA_API_KEY": ""}):
        result = await keepa_fetch_product("B00000", domain=1)
        assert result["status"] == "error"
        assert "KEEPA_API_KEY" in result["message"]


@pytest.mark.asyncio
async def test_fetch_product_returns_summary():
    mock_response_data = {
        "tokensLeft": 95,
        "products": [
            {
                "asin": "B00TEST",
                "title": "Test Product",
                "brand": "TestBrand",
                "categoryTree": [{"name": "Home"}, {"name": "Bedding"}],
                "monthlySold": 500,
                "csv": [
                    [100, 2000, 200, 1500],  # Amazon (idx 0)
                    [100, 2100, 200, 1600],  # New (idx 1)
                ],
                "coupon": 100,
                "offers": [],
            }
        ]
    }

    mock_resp = SyncMagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_response_data
    mock_resp.raise_for_status = SyncMagicMock()

    with patch.dict(os.environ, {"KEEPA_API_KEY": "fake_key"}):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [_MOCK_TOKEN_RESP, mock_resp]
            with patch("app.tools.keepa_api._load_cached", return_value=None):
                with patch("app.tools.keepa_api._save_cache"):
                    result = await keepa_fetch_product("B00TEST", domain=1)

                    assert result["status"] == "success"
                    assert result["asin"] == "B00TEST"
                    assert result["title"] == "Test Product"
                    assert result["current_prices"]["amazon"] == 15.0
                    # No massive raw data in response
                    assert "csv" not in result
                    assert "offers" not in str(result) or "hint" in result
