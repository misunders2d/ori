"""Classify uploaded files and prepare them for the LLM.

Transport-level helper — owns the full decision tree for "what do I do with
this file?" so every transport (Slack, Telegram, future adapters) makes the
same choice.

Decision tree:
1. If the MIME maps to a text extractor (Excel, CSV, DOCX, PPTX, RTF, plain
   text) → extract text, return as a text attachment. Large files are
   truncated with a summary header so the agent knows to use `analyze_data`
   for deeper inspection.
2. Else if the MIME is in the whitelist of Gemini-natively-supported types
   (PDF, images, video, audio) → return as an inline_data blob alongside a
   "[Media: ...]" text marker.
3. Else → return a user-facing rejection message explaining how to proceed
   (convert to PDF, paste text, use analyze_data on the saved path).

Large files (>20 MB) are a transport concern, not this module's — pollers
should enforce that cap before calling here.
"""

import csv
import io
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MIME sets
# ---------------------------------------------------------------------------

_SPREADSHEET_MIMES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
    "application/vnd.ms-excel",  # .xls (often served as .xlsx by Google; openpyxl handles xlsx only)
}

_CSV_MIMES = {
    "text/csv",
    "application/csv",
}

_DOCX_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
}

_PPTX_MIMES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
}

_RTF_MIMES = {
    "application/rtf",
    "text/rtf",
}

_PLAIN_TEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/json",
    "application/x-yaml",
    "text/yaml",
    "text/x-yaml",
}

# MIMEs we can extract to text server-side before the LLM sees the content.
_CONVERTIBLE_MIMES = (
    _SPREADSHEET_MIMES
    | _CSV_MIMES
    | _DOCX_MIMES
    | _PPTX_MIMES
    | _RTF_MIMES
    | _PLAIN_TEXT_MIMES
)

# MIMEs Gemini parses natively via Part.inline_data. Anything outside this
# AND not text-convertible gets a polite "please convert" rejection instead
# of being blasted at the model as an opaque blob.
_SUPPORTED_INLINE_MIMES = {
    # Documents
    "application/pdf",
    # Images
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
    # Video
    "video/mp4",
    "video/quicktime",  # .mov
    "video/webm",
    "video/x-msvideo",  # .avi
    "video/mpeg",
    "video/x-flv",
    "video/3gpp",
    # Audio
    "audio/mpeg",  # .mp3
    "audio/wav",
    "audio/x-wav",
    "audio/flac",
    "audio/mp4",  # .m4a
    "audio/aac",
    "audio/ogg",
    "audio/webm",
    "audio/aiff",
    "audio/x-aiff",
}

_UPLOADS_DIR = os.path.abspath("./tmp/uploads")

# Max rows to include inline — beyond this, show preview + summary.
_MAX_PREVIEW_ROWS = 30

# Max characters of extracted prose before truncation (DOCX, PPTX, RTF, plain).
# Roughly ~15-20 thousand tokens at 1 token ≈ 4 chars — fits comfortably in
# most context windows while leaving room for response + tool calls.
_MAX_PROSE_CHARS = 60_000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class PreparedFile:
    """What to pass to the LLM for a given uploaded file.

    ``text`` is always present — it's either extracted content, a media
    marker line, or a user-facing rejection message. ``inline_blob`` is the
    optional raw-bytes attachment for Gemini-supported binary formats; callers
    wrap it in ``Part(inline_data=Blob(...))``.
    """
    text: str
    inline_blob: tuple[bytes, str] | None = None  # (bytes, mime_type)


def save_upload(data: bytes, filename: str) -> str:
    """Save uploaded file to disk for later analysis. Returns the file path.

    Avoids overwriting by appending a counter if the filename already exists.
    """
    os.makedirs(_UPLOADS_DIR, exist_ok=True)
    # Use only the filename component to avoid directory traversal from hostile
    # transports; strip leading dots and path separators.
    safe_name = os.path.basename(filename.strip()) or "attachment"
    path = os.path.join(_UPLOADS_DIR, safe_name)
    base, ext = os.path.splitext(safe_name)
    counter = 1
    while os.path.exists(path):
        path = os.path.join(_UPLOADS_DIR, f"{base}_{counter}{ext}")
        counter += 1
    with open(path, "wb") as f:
        f.write(data)
    return path


def is_convertible(mime_type: str) -> bool:
    """True if this MIME can be extracted to text server-side.

    Kept for backward compatibility with transports that pre-date
    ``prepare_for_llm``; new code should call ``prepare_for_llm`` directly.
    """
    return mime_type in _CONVERTIBLE_MIMES


def to_text(data: bytes, mime_type: str, filename: str = "") -> str | None:
    """Extract text from a supported binary file. Returns None on failure.

    Kept for backward compat. New code should use ``prepare_for_llm``.
    """
    try:
        if mime_type in _SPREADSHEET_MIMES:
            return _excel_to_text(data, filename)
        if mime_type in _CSV_MIMES:
            return _csv_to_text(data, filename)
        if mime_type in _DOCX_MIMES:
            return _docx_to_text(data, filename)
        if mime_type in _PPTX_MIMES:
            return _pptx_to_text(data, filename)
        if mime_type in _RTF_MIMES:
            return _rtf_to_text(data, filename)
        if mime_type in _PLAIN_TEXT_MIMES:
            return _plain_to_text(data, filename)
    except Exception:
        logger.exception("Failed to convert %s (%s) to text", filename, mime_type)
    return None


def prepare_for_llm(
    data: bytes,
    mime_type: str,
    filename: str,
    saved_path: str = "",
) -> PreparedFile:
    """Decide how to present a file to the LLM. Called by transport adapters.

    - Convertible MIME → extracts content, wraps with ``[File: ... | saved ...]``
      header.
    - Natively-supported binary MIME → ``[Media: ...]`` marker + inline_blob.
    - Otherwise → rejection message explaining what the user should do.
    """
    path_suffix = f" | saved to: {saved_path}" if saved_path else ""

    # Branch 1: extractable text content.
    if mime_type in _CONVERTIBLE_MIMES:
        extracted = to_text(data, mime_type, filename)
        header = f"[File: {filename}{path_suffix}]"
        if extracted:
            return PreparedFile(text=f"{header}\n{extracted}")
        # Extractor said yes but parsing failed — fall through to a clear
        # message instead of a silent blob that Gemini can't read.
        return PreparedFile(
            text=(
                f"{header} (content extraction failed — the file may be "
                "corrupt, password-protected, or in a legacy format)."
            )
        )

    # Branch 2: natively-supported binary → inline_data.
    if mime_type in _SUPPORTED_INLINE_MIMES:
        return PreparedFile(
            text=f"[Media: {filename}{path_suffix}]",
            inline_blob=(data, mime_type),
        )

    # Branch 3: unsupported.
    return PreparedFile(
        text=(
            f"[File received: {filename} ({mime_type}){path_suffix}]\n"
            f"I cannot read this format directly. Options:\n"
            f"- If the file is mostly text (old .doc, .odt, legacy formats), export it to PDF or DOCX and re-upload.\n"
            f"- Paste the relevant content into the chat.\n"
            f"- Ask me to use `analyze_data` on the saved path if it's structured data."
        ),
    )


# ---------------------------------------------------------------------------
# Individual converters
# ---------------------------------------------------------------------------


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


def _docx_to_text(data: bytes, filename: str) -> str:
    """Extract paragraph text from a .docx file."""
    import docx  # python-docx

    doc = docx.Document(io.BytesIO(data))
    paragraphs = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    # Tables — walk rows and cells, include as tab-separated lines.
    for table in doc.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                paragraphs.append("\t".join(cells))
    full = "\n".join(paragraphs)
    return _truncate_prose(full, filename, "DOCX")


def _pptx_to_text(data: bytes, filename: str) -> str:
    """Extract slide title + body text from a .pptx file."""
    from pptx import Presentation  # python-pptx

    prs = Presentation(io.BytesIO(data))
    slide_blocks = []
    for idx, slide in enumerate(prs.slides, start=1):
        pieces = [f"--- Slide {idx} ---"]
        for shape in slide.shapes:
            # Text frames (titles, bullets, arbitrary text boxes).
            if hasattr(shape, "text_frame") and shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    pieces.append(text)
            # Tables inside slides.
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if any(cells):
                        pieces.append("\t".join(cells))
        if len(pieces) > 1:  # More than just the header
            slide_blocks.append("\n".join(pieces))
    full = "\n\n".join(slide_blocks) if slide_blocks else "(no extractable text in slides)"
    return _truncate_prose(full, filename, "PPTX")


def _rtf_to_text(data: bytes, filename: str) -> str:
    """Strip RTF control codes and return plain text."""
    from striprtf.striprtf import rtf_to_text

    raw = data.decode("utf-8", errors="replace")
    text = rtf_to_text(raw)
    return _truncate_prose(text, filename, "RTF")


def _plain_to_text(data: bytes, filename: str) -> str:
    """Decode a plain-text-like file (txt, md, json, yaml)."""
    text = data.decode("utf-8", errors="replace")
    return _truncate_prose(text, filename, "TEXT")


def _truncate_prose(text: str, filename: str, kind: str) -> str:
    """Cap extracted prose so very long documents don't blow the context."""
    if len(text) <= _MAX_PROSE_CHARS:
        return text
    kept = text[:_MAX_PROSE_CHARS]
    dropped = len(text) - _MAX_PROSE_CHARS
    return (
        f"[Large {kind}: {len(text)} chars total; showing first "
        f"{_MAX_PROSE_CHARS}. {dropped} chars truncated.]\n\n{kept}"
    )
