"""Convert binary file formats (Excel, CSV) to text for LLM consumption.

Large files are truncated with a summary header so the agent knows to use
the analyze_data tool for deeper inspection.
"""

import csv
import io
import logging
import os

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

_UPLOADS_DIR = os.path.abspath("./tmp/uploads")

# Max rows to include inline — beyond this, show preview + summary
_MAX_PREVIEW_ROWS = 30


def save_upload(data: bytes, filename: str) -> str:
    """Save uploaded file to disk for later analysis. Returns the file path."""
    os.makedirs(_UPLOADS_DIR, exist_ok=True)
    path = os.path.join(_UPLOADS_DIR, filename)
    # Avoid overwriting — append counter if file exists
    base, ext = os.path.splitext(filename)
    counter = 1
    while os.path.exists(path):
        path = os.path.join(_UPLOADS_DIR, f"{base}_{counter}{ext}")
        counter += 1
    with open(path, "wb") as f:
        f.write(data)
    return path


def is_convertible(mime_type: str) -> bool:
    """Check if a MIME type can be converted to text."""
    return mime_type in _CONVERTIBLE_MIMES


def to_text(data: bytes, mime_type: str, filename: str = "") -> str | None:
    """Convert binary spreadsheet/CSV data to text. Returns None on failure."""
    try:
        if mime_type in _SPREADSHEET_MIMES:
            return _excel_to_text(data, filename)
        if mime_type in _CSV_MIMES:
            return _csv_to_text(data, filename)
    except Exception:
        logger.exception("Failed to convert %s (%s) to text", filename, mime_type)
    return None


def _csv_to_text(data: bytes, filename: str) -> str:
    """Convert CSV bytes to text with truncation for large files."""
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) <= _MAX_PREVIEW_ROWS + 1:  # +1 for header
        return text

    header = lines[0]
    col_count = len(header.split(","))
    preview = "\n".join(lines[:_MAX_PREVIEW_ROWS + 1])
    return (
        f"[Large CSV: {len(lines) - 1} rows, ~{col_count} columns]\n"
        f"[Showing first {_MAX_PREVIEW_ROWS} rows. Use analyze_data tool for full analysis.]\n\n"
        f"{preview}\n... ({len(lines) - 1 - _MAX_PREVIEW_ROWS} more rows)"
    )


def _excel_to_text(data: bytes, filename: str) -> str:
    """Convert Excel bytes to a readable text representation."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        total_rows = len(rows)
        truncated = total_rows > _MAX_PREVIEW_ROWS + 1  # +1 for header

        buf = io.StringIO()
        writer = csv.writer(buf)
        display_rows = rows[:_MAX_PREVIEW_ROWS + 1] if truncated else rows
        for row in display_rows:
            writer.writerow([cell if cell is not None else "" for cell in row])

        header = f"--- Sheet: {sheet_name} ({total_rows - 1} rows, {len(rows[0])} columns) ---"
        if truncated:
            header += (
                f"\n[Showing first {_MAX_PREVIEW_ROWS} rows. "
                f"Use analyze_data tool for full analysis.]"
            )
            parts.append(f"{header}\n{buf.getvalue()}... ({total_rows - 1 - _MAX_PREVIEW_ROWS} more rows)")
        else:
            parts.append(f"{header}\n{buf.getvalue()}")

    wb.close()
    return "\n".join(parts) if parts else "(empty spreadsheet)"
