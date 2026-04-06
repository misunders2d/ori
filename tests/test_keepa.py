import pytest
import os
from unittest.mock import AsyncMock, patch, MagicMock as SyncMagicMock

from app.tools.keepa_api import (
    keepa_get_product_data,
    get_latest_price_from_csv
)

@pytest.mark.asyncio
async def test_get_latest_price_from_csv():
    # 2 items per row: [time, price]
    csv = [100, 1000, 200, 2000, 300, -1]
    assert get_latest_price_from_csv(csv, 2) == 20.0
    
    # 3 items per row: [time, price, shipping]
    csv = [100, 1000, 50, 200, 2000, 60]
    assert get_latest_price_from_csv(csv, 3) == 20.0

@pytest.mark.asyncio
async def test_keepa_get_product_data_no_key():
    with patch.dict(os.environ, {"KEEPA_API_KEY": ""}):
        result = await keepa_get_product_data("B00000", domain=1)
        assert result["status"] == "error"
        assert "KEEPA_API_KEY" in result["message"]

@pytest.mark.asyncio
async def test_keepa_get_product_data_success():
    mock_response_data = {
        "products": [
            {
                "asin": "B00000",
                "title": "Test Product",
                "csv": [
                    [100, 2000, 200, 1500], # Amazon (Index 0)
                    [100, 2100, 200, 1600], # New (Index 1)
                ],
                "coupon": 100, # $1.00 off
                "offers": [
                    {
                        "isPrimeExcl": True,
                        "primeExclCSV": [100, 1400],
                        "offerCSV": [100, 1400, 0]
                    }
                ]
            }
        ]
    }
    
    # Mock Response object
    mock_resp = SyncMagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_response_data
    mock_resp.raise_for_status = SyncMagicMock()

    with patch.dict(os.environ, {"KEEPA_API_KEY": "fake_key"}):
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp
            
            result = await keepa_get_product_data("B00000", domain=1)
            
            assert result["status"] == "success"
            assert result["asin"] == "B00000"
            pricing = result["pricing"]
            assert pricing["amazon_price"] == 15.0
            assert pricing["new_price"] == 16.0
            assert pricing["prime_exclusive_price"] == 14.0
            # Best offer is 14.0 (Prime Exclusive). 
            # Coupon is 1.0. 
            # 14.0 - 1.0 = 13.0
            assert pricing["best_current_offer"] == 13.0
