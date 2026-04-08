"""Tests for SP-API direct report-to-CSV pipeline."""

import json
import os

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.tools.sp_api_export import (
    data_to_csv,
    _parse_flat_file,
    _parse_json_report,
    _save_csv,
)

_EXPORTS_DIR = os.path.abspath("./tmp/exports")


@pytest.fixture(autouse=True)
def ensure_exports_dir():
    os.makedirs(_EXPORTS_DIR, exist_ok=True)
    yield


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

class TestParseFlatFile:

    def test_basic_tsv(self):
        text = "col1\tcol2\tcol3\nval1\tval2\tval3\nval4\tval5\tval6"
        records = _parse_flat_file(text)
        assert len(records) == 2
        assert records[0]["col1"] == "val1"
        assert records[1]["col3"] == "val6"

    def test_empty_content(self):
        records = _parse_flat_file("")
        assert records == []

    def test_header_only(self):
        records = _parse_flat_file("col1\tcol2")
        assert records == []


class TestParseJsonReport:

    def test_plain_list(self):
        text = json.dumps([{"a": 1}, {"a": 2}])
        records = _parse_json_report(text)
        assert len(records) == 2

    def test_nested_report_data(self):
        text = json.dumps({"reportData": [{"x": 1}, {"x": 2}]})
        records = _parse_json_report(text)
        assert len(records) == 2
        assert records[0]["x"] == 1

    def test_single_dict(self):
        text = json.dumps({"foo": "bar"})
        records = _parse_json_report(text)
        assert len(records) == 1
        assert records[0]["foo"] == "bar"


# ---------------------------------------------------------------------------
# _save_csv
# ---------------------------------------------------------------------------

class TestSaveCsv:

    def test_basic_save(self):
        records = [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}]
        path = _save_csv(records, "test_save.csv")
        assert os.path.isfile(path)
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) == 3  # header + 2 rows
        assert "name" in lines[0]
        os.remove(path)

    def test_empty_records(self):
        path = _save_csv([], "empty.csv")
        assert os.path.isfile(path)
        os.remove(path)

    def test_heterogeneous_keys(self):
        records = [{"a": 1, "b": 2}, {"a": 3, "c": 4}]
        path = _save_csv(records, "hetero.csv")
        with open(path) as f:
            header = f.readline().strip()
        assert "a" in header
        assert "b" in header
        assert "c" in header
        os.remove(path)


# ---------------------------------------------------------------------------
# data_to_csv
# ---------------------------------------------------------------------------

class TestDataToCsv:

    @pytest.mark.asyncio
    async def test_basic_json_array(self):
        data = json.dumps([
            {"sku": "ABC123", "qty": 10, "price": 29.99},
            {"sku": "DEF456", "qty": 5, "price": 49.99},
        ])
        result = await data_to_csv(data, "test_data.csv")
        assert result["status"] == "success"
        assert result["rows"] == 2
        assert result["columns"] == 3
        assert os.path.isfile(result["file_path"])
        os.remove(result["file_path"])

    @pytest.mark.asyncio
    async def test_wrapped_in_items_key(self):
        data = json.dumps({"items": [{"a": 1}, {"a": 2}]})
        result = await data_to_csv(data, "wrapped.csv")
        assert result["status"] == "success"
        assert result["rows"] == 2
        os.remove(result["file_path"])

    @pytest.mark.asyncio
    async def test_nested_values_flattened(self):
        data = json.dumps([{"name": "test", "details": {"x": 1, "y": 2}}])
        result = await data_to_csv(data, "nested.csv")
        assert result["status"] == "success"
        # Nested dict should be JSON-stringified (CSV may escape quotes)
        with open(result["file_path"]) as f:
            content = f.read()
        assert "x" in content and "y" in content
        os.remove(result["file_path"])

    @pytest.mark.asyncio
    async def test_invalid_json(self):
        result = await data_to_csv("not json at all", "bad.csv")
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_empty_array(self):
        result = await data_to_csv("[]", "empty.csv")
        assert result["status"] == "error"
        assert "empty" in result["message"]

    @pytest.mark.asyncio
    async def test_auto_csv_extension(self):
        data = json.dumps([{"a": 1}])
        result = await data_to_csv(data, "noext")
        assert result["filename"] == "noext.csv"
        os.remove(result["file_path"])


# ---------------------------------------------------------------------------
# export_report_to_csv (mocked SP-API)
# ---------------------------------------------------------------------------

class TestExportReportToCsv:

    @pytest.mark.asyncio
    async def test_not_configured(self, monkeypatch):
        from app.tools.sp_api_export import export_report_to_csv
        monkeypatch.delenv("SP_API_CLIENT_ID", raising=False)
        monkeypatch.delenv("SP_API_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("SP_API_REFRESH_TOKEN", raising=False)
        result = await export_report_to_csv("GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA")
        assert result["status"] == "error"
        assert "not configured" in result["message"]

    @pytest.mark.asyncio
    @patch("app.tools.sp_api_export._POLL_INTERVAL", 0.01)
    @patch("app.tools.sp_api_export._sp_call", new_callable=AsyncMock)
    async def test_full_pipeline(self, mock_sp_call, monkeypatch):
        from app.tools.sp_api_export import export_report_to_csv

        monkeypatch.setenv("SP_API_CLIENT_ID", "test")
        monkeypatch.setenv("SP_API_CLIENT_SECRET", "test")
        monkeypatch.setenv("SP_API_REFRESH_TOKEN", "test")

        # Mock: create_report → get_report (DONE) → get_report_document
        mock_sp_call.side_effect = [
            {"reportId": "RPT-123"},  # create_report
            {"processingStatus": "DONE", "reportDocumentId": "DOC-456"},  # get_report
            {"document": "sku\tqty\tprice\nABC\t10\t29.99\nDEF\t5\t49.99"},  # get_report_document
        ]

        result = await export_report_to_csv("GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA", days=7)

        assert result["status"] == "success"
        assert result["rows"] == 2
        assert result["columns"] == 3
        assert os.path.isfile(result["file_path"])

        # Verify CSV content
        with open(result["file_path"]) as f:
            lines = f.readlines()
        assert "sku" in lines[0]
        assert "ABC" in lines[1]

        os.remove(result["file_path"])

    @pytest.mark.asyncio
    @patch("app.tools.sp_api_export._POLL_INTERVAL", 0.01)
    @patch("app.tools.sp_api_export._sp_call", new_callable=AsyncMock)
    async def test_report_failure(self, mock_sp_call, monkeypatch):
        from app.tools.sp_api_export import export_report_to_csv

        monkeypatch.setenv("SP_API_CLIENT_ID", "test")
        monkeypatch.setenv("SP_API_CLIENT_SECRET", "test")
        monkeypatch.setenv("SP_API_REFRESH_TOKEN", "test")

        mock_sp_call.side_effect = [
            {"reportId": "RPT-123"},
            {"processingStatus": "FATAL"},
        ]

        result = await export_report_to_csv("GET_BAD_REPORT")
        assert result["status"] == "error"
        assert "FATAL" in result["message"]

    @pytest.mark.asyncio
    @patch("app.tools.sp_api_export._POLL_INTERVAL", 0.01)
    @patch("app.tools.sp_api_export._sp_call", new_callable=AsyncMock)
    async def test_json_report(self, mock_sp_call, monkeypatch):
        from app.tools.sp_api_export import export_report_to_csv

        monkeypatch.setenv("SP_API_CLIENT_ID", "test")
        monkeypatch.setenv("SP_API_CLIENT_SECRET", "test")
        monkeypatch.setenv("SP_API_REFRESH_TOKEN", "test")

        json_data = json.dumps([
            {"asin": "B001", "clicks": 100, "sales": 50},
            {"asin": "B002", "clicks": 200, "sales": 75},
        ])

        mock_sp_call.side_effect = [
            {"reportId": "RPT-789"},
            {"processingStatus": "DONE", "reportDocumentId": "DOC-012"},
            {"document": json_data},
        ]

        result = await export_report_to_csv("GET_BRAND_ANALYTICS_SEARCH_CATALOG_PERFORMANCE_REPORT")

        assert result["status"] == "success"
        assert result["rows"] == 2
        os.remove(result["file_path"])
