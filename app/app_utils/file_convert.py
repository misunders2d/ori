"""Convert binary file formats (Excel, CSV) to text for LLM consumption."""

import csv
import io
import logging

logger = logging.getLogger(__name__)

_SPREADSHEET_MIMES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
    "application/vnd.ms-excel",  # .xls
}

_CSV_MIMES = {
    "text/csv",
    "application/csv",
}

_CONVERTIBLE_MIMES = _SPREADSHEET_MIMES | _CSV_MIMES


def is_convertible(mime_type: str) -> bool:
    """Check if a MIME type can be converted to text."""
    return mime_type in _CONVERTIBLE_MIMES


def to_text(data: bytes, mime_type: str, filename: str = "") -> str | None:
    """Convert binary spreadsheet/CSV data to text. Returns None on failure."""
    try:
        if mime_type in _SPREADSHEET_MIMES:
            return _excel_to_text(data)
        if mime_type in _CSV_MIMES:
            return data.decode("utf-8", errors="replace")
    except Exception:
        logger.exception("Failed to convert %s (%s) to text", filename, mime_type)
    return None


def _excel_to_text(data: bytes) -> str:
    """Convert Excel bytes to a readable text representation."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in rows:
            writer.writerow([cell if cell is not None else "" for cell in row])

        parts.append(f"--- Sheet: {sheet_name} ---\n{buf.getvalue()}")

    wb.close()
    return "\n".join(parts) if parts else "(empty spreadsheet)"
