"""PowerPoint (.pptx) generation.

Generic deck builder: ads, sales, warehouse, inventory, ASIN audit, weekly
business summary — all compose from the same eight slide layouts. Domain-
specific content is the agent's job; this tool just renders.

Brand-template behaviour: ``data/presentations/templates/<name>.pptx``
(default ``default.pptx``) is opened as the starting Presentation if it
exists, preserving the template's slide masters, color theme, fonts, and
any logo placement on the master slide. If no template is found, a blank
``Presentation()`` is used and the agent picks layouts from the python-
pptx default theme. Either way the output is a fully-self-contained
.pptx file that the user can open in PowerPoint / Slides / Keynote.

Delivery: the returned ``file_path`` flows through ``file_attachment_capture``
+ ``file_attachment_inject`` (see ``app/callbacks/guardrails.py``) so the
deck is auto-attached to the agent's response on Slack / Telegram / A2A.
No Drive upload is performed in this build; the deck is delivered as a
chat attachment only. If the user asks "upload this to Drive," surface
that limitation explicitly — there is no Drive-upload primitive in
``GoogleWorkspaceToolset`` (only read-only ``drive_list_files`` /
``drive_download_file``). Do NOT fabricate a Drive-upload tool name.
Production proof 2026-05-20 (cron_97f22322 incident): a prior version of
this docstring + ``docs/PRESENTATIONS.md`` + ``skills/presentation-skill/
SKILL.md`` told the LLM that ``drive_upload_file`` was a callable tool;
when the cron asked it to "upload the CSV to Google Drive" the LLM
fabricated "Drive upload was bypassed as the account is not connected"
as a cover story. See ``docs/RUNBOOK.md §12`` for the fabrication-
detection defenses and the operator playbook.

Slide specs are dicts:

    {"layout": "title",          "title": str,  "subtitle": str?}
    {"layout": "bullets",        "title": str,  "bullets": list[str]}
    {"layout": "chart",          "title": str,  "chart_path": str,  "caption": str?}
    {"layout": "kpi_grid",       "title": str,  "kpis": list[{label, value, delta?}]}
    {"layout": "table",          "title": str,  "columns": list[str], "rows": list[list[str]]}
    {"layout": "two_column",     "title": str,  "left": str,  "right": str}
    {"layout": "image",          "title": str,  "image_path": str,  "caption": str?}
    {"layout": "text",           "title": str,  "paragraph": str}

See ``docs/PRESENTATIONS.md`` for the authoring rationale and brand-template
guidance.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


_TEMPLATES_DIR = os.path.abspath("./data/presentations/templates")
_EXPORTS_DIR = os.path.abspath("./data/exports/presentations")

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


# ---------------------------------------------------------------------------
# File-path helpers
# ---------------------------------------------------------------------------


def _slugify(title: str) -> str:
    base = title.strip().lower().replace(" ", "-")
    base = _SLUG_RE.sub("-", base)
    # Collapse runs of `-` introduced by adjacent non-allowed chars
    # (e.g. ``" — "`` → space-`-`-space-`-`-space) so the slug stays
    # readable. ``Weekly Sales — Q2'26!`` becomes ``weekly-sales-q2-26``,
    # not ``weekly-sales---q2-26-``.
    base = re.sub(r"-+", "-", base).strip("-")
    return base or "deck"


def _output_path(filename: str, title: str) -> str:
    os.makedirs(_EXPORTS_DIR, exist_ok=True)
    if filename:
        name = filename if filename.endswith(".pptx") else f"{filename}.pptx"
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"{_slugify(title)}-{ts}-{uuid.uuid4().hex[:6]}.pptx"
    return os.path.join(_EXPORTS_DIR, name)


def _resolve_template(template_name: str) -> Optional[str]:
    """Return the template file to start the Presentation from, or None
    to use a blank deck. ``template_name`` may be empty (try
    ``default.pptx``), or a bare name (resolve to ``<name>.pptx``)."""
    candidates: list[str] = []
    if template_name:
        nm = template_name if template_name.endswith(".pptx") else f"{template_name}.pptx"
        candidates.append(nm)
    candidates.append("default.pptx")

    for cand in candidates:
        p = os.path.join(_TEMPLATES_DIR, cand)
        if os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


_KNOWN_LAYOUTS = {
    "title",
    "bullets",
    "chart",
    "kpi_grid",
    "table",
    "two_column",
    "image",
    "text",
}


def _pick_layout(prs, name: str):
    """Map a friendly layout name to the closest python-pptx layout in
    the (possibly branded) deck. python-pptx's default layouts:
      0: Title (Title + Subtitle)
      1: Title and Content (bullets)
      5: Title Only
      6: Blank
    Branded templates may have different ordering; we fall back to
    "Blank" (index 6) when the named layout isn't found, then add text
    via positioned text boxes.
    """
    preferred = {
        "title": 0,
        "bullets": 1,
        "chart": 5,
        "kpi_grid": 5,
        "table": 5,
        "two_column": 5,
        "image": 5,
        "text": 1,
    }
    idx = preferred.get(name, 6)
    if idx >= len(prs.slide_layouts):
        idx = min(6, len(prs.slide_layouts) - 1)
    return prs.slide_layouts[idx]


def _add_title(slide, title: str) -> None:
    if not title:
        return
    if slide.shapes.title is not None:
        slide.shapes.title.text = title
        return
    from pptx.util import Inches, Pt

    box = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(9), Inches(0.7))
    tf = box.text_frame
    tf.text = title
    for p in tf.paragraphs:
        for r in p.runs:
            r.font.size = Pt(28)
            r.font.bold = True


def _add_title_slide(prs, spec: dict) -> None:
    slide = prs.slides.add_slide(_pick_layout(prs, "title"))
    _add_title(slide, spec.get("title", ""))
    subtitle = spec.get("subtitle") or ""
    if not subtitle:
        return
    # python-pptx default layout 0 has a subtitle placeholder at index 1.
    for ph in slide.placeholders:
        if ph.placeholder_format.idx == 1:
            ph.text = subtitle
            return
    # Fallback: add a positioned text box.
    from pptx.util import Inches, Pt

    box = slide.shapes.add_textbox(Inches(0.5), Inches(2.0), Inches(9), Inches(1.0))
    box.text_frame.text = subtitle
    for r in box.text_frame.paragraphs[0].runs:
        r.font.size = Pt(18)


def _add_bullets_slide(prs, spec: dict) -> None:
    slide = prs.slides.add_slide(_pick_layout(prs, "bullets"))
    _add_title(slide, spec.get("title", ""))
    bullets = spec.get("bullets") or []
    # Find the content placeholder (idx 1 on layout 1).
    content_ph = None
    for ph in slide.placeholders:
        if ph.placeholder_format.idx == 1:
            content_ph = ph
            break
    if content_ph is None:
        from pptx.util import Inches

        box = slide.shapes.add_textbox(Inches(0.5), Inches(1.2), Inches(9), Inches(5.5))
        content_ph = box

    tf = content_ph.text_frame
    tf.clear()
    for i, b in enumerate(bullets):
        if i == 0:
            tf.text = str(b)
        else:
            tf.add_paragraph().text = str(b)


def _add_chart_slide(prs, spec: dict) -> None:
    slide = prs.slides.add_slide(_pick_layout(prs, "chart"))
    _add_title(slide, spec.get("title", ""))
    chart_path = spec.get("chart_path") or ""
    if chart_path and os.path.isfile(chart_path):
        from pptx.util import Inches

        slide.shapes.add_picture(
            chart_path, Inches(0.5), Inches(1.2), width=Inches(9), height=Inches(5)
        )
    caption = spec.get("caption") or ""
    if caption:
        from pptx.util import Inches, Pt

        box = slide.shapes.add_textbox(Inches(0.5), Inches(6.3), Inches(9), Inches(0.7))
        box.text_frame.text = caption
        for r in box.text_frame.paragraphs[0].runs:
            r.font.size = Pt(12)
            r.font.italic = True


def _add_kpi_grid_slide(prs, spec: dict) -> None:
    """KPI grid — auto 2×N tiles, each tile is ``{label, value, delta?}``.

    Each tile renders as a text box with label (small) + value (large)
    + optional delta (small, coloured red/green/neutral). Up to 6 KPIs
    fit comfortably; more get squeezed via the auto-grid math.
    """
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    slide = prs.slides.add_slide(_pick_layout(prs, "kpi_grid"))
    _add_title(slide, spec.get("title", ""))
    kpis = list(spec.get("kpis") or [])
    if not kpis:
        return

    cols = 3 if len(kpis) > 4 else (2 if len(kpis) > 2 else len(kpis))
    rows = (len(kpis) + cols - 1) // cols
    margin = 0.5
    gap = 0.25
    tile_w = (10 - 2 * margin - gap * (cols - 1)) / cols
    tile_h = (5 - gap * (rows - 1)) / rows

    for i, kpi in enumerate(kpis):
        if not isinstance(kpi, dict):
            continue
        r, c = divmod(i, cols)
        left = Inches(margin + c * (tile_w + gap))
        top = Inches(1.3 + r * (tile_h + gap))
        box = slide.shapes.add_textbox(left, top, Inches(tile_w), Inches(tile_h))
        tf = box.text_frame
        tf.word_wrap = True
        tf.text = str(kpi.get("label") or "")
        for run in tf.paragraphs[0].runs:
            run.font.size = Pt(12)
            run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
        value_p = tf.add_paragraph()
        value_p.text = str(kpi.get("value") or "")
        value_p.alignment = PP_ALIGN.LEFT
        for run in value_p.runs:
            run.font.size = Pt(28)
            run.font.bold = True
        delta = kpi.get("delta")
        if delta is not None and str(delta).strip():
            d = str(delta)
            d_p = tf.add_paragraph()
            d_p.text = d
            for run in d_p.runs:
                run.font.size = Pt(12)
                if d.startswith("+") or d.startswith("▲"):
                    run.font.color.rgb = RGBColor(0x1F, 0x9D, 0x55)
                elif d.startswith("-") or d.startswith("▼"):
                    run.font.color.rgb = RGBColor(0xC0, 0x39, 0x2B)


def _add_table_slide(prs, spec: dict) -> None:
    from pptx.util import Inches, Pt

    slide = prs.slides.add_slide(_pick_layout(prs, "table"))
    _add_title(slide, spec.get("title", ""))
    columns = list(spec.get("columns") or [])
    rows = [list(r) for r in (spec.get("rows") or []) if isinstance(r, (list, tuple))]
    if not columns or not rows:
        return

    n_rows = len(rows) + 1  # +1 for header
    n_cols = len(columns)
    table_shape = slide.shapes.add_table(
        n_rows, n_cols, Inches(0.5), Inches(1.3), Inches(9), Inches(5)
    )
    table = table_shape.table

    for ci, col in enumerate(columns):
        cell = table.cell(0, ci)
        cell.text = str(col)
        for p in cell.text_frame.paragraphs:
            for r in p.runs:
                r.font.bold = True
                r.font.size = Pt(12)

    for ri, row in enumerate(rows, start=1):
        for ci in range(n_cols):
            val = row[ci] if ci < len(row) else ""
            cell = table.cell(ri, ci)
            cell.text = str(val)
            for p in cell.text_frame.paragraphs:
                for r in p.runs:
                    r.font.size = Pt(11)


def _add_two_column_slide(prs, spec: dict) -> None:
    from pptx.util import Inches, Pt

    slide = prs.slides.add_slide(_pick_layout(prs, "two_column"))
    _add_title(slide, spec.get("title", ""))

    def _col(left_in: float, content: str) -> None:
        if not content:
            return
        box = slide.shapes.add_textbox(
            Inches(left_in), Inches(1.3), Inches(4.5), Inches(5)
        )
        box.text_frame.word_wrap = True
        box.text_frame.text = content
        for p in box.text_frame.paragraphs:
            for r in p.runs:
                r.font.size = Pt(14)

    _col(0.5, str(spec.get("left") or ""))
    _col(5.25, str(spec.get("right") or ""))


def _add_image_slide(prs, spec: dict) -> None:
    from pptx.util import Inches, Pt

    slide = prs.slides.add_slide(_pick_layout(prs, "image"))
    _add_title(slide, spec.get("title", ""))
    image_path = spec.get("image_path") or ""
    if image_path and os.path.isfile(image_path):
        slide.shapes.add_picture(
            image_path, Inches(0.5), Inches(1.2), width=Inches(9), height=Inches(5)
        )
    caption = spec.get("caption") or ""
    if caption:
        box = slide.shapes.add_textbox(Inches(0.5), Inches(6.3), Inches(9), Inches(0.7))
        box.text_frame.text = caption
        for r in box.text_frame.paragraphs[0].runs:
            r.font.size = Pt(12)
            r.font.italic = True


def _add_text_slide(prs, spec: dict) -> None:
    from pptx.util import Inches, Pt

    slide = prs.slides.add_slide(_pick_layout(prs, "text"))
    _add_title(slide, spec.get("title", ""))
    paragraph = str(spec.get("paragraph") or "")
    if not paragraph:
        return
    box = slide.shapes.add_textbox(Inches(0.5), Inches(1.3), Inches(9), Inches(5))
    box.text_frame.word_wrap = True
    box.text_frame.text = paragraph
    for p in box.text_frame.paragraphs:
        for r in p.runs:
            r.font.size = Pt(16)


_LAYOUT_DISPATCH = {
    "title": _add_title_slide,
    "bullets": _add_bullets_slide,
    "chart": _add_chart_slide,
    "kpi_grid": _add_kpi_grid_slide,
    "table": _add_table_slide,
    "two_column": _add_two_column_slide,
    "image": _add_image_slide,
    "text": _add_text_slide,
}


# ---------------------------------------------------------------------------
# Public tool
# ---------------------------------------------------------------------------


def generate_presentation(
    title: str,
    slides: list[dict],
    tool_context: ToolContext = None,
    filename: str = "",
    template_name: str = "",
) -> dict:
    """Build a PowerPoint deck and save it to disk. The returned file_path
    is auto-attached to the agent's response by ``file_attachment_inject``
    (no extra steps needed for Slack / Telegram / A2A delivery).

    Args:
        title: Deck title (also used in the default filename when ``filename`` is empty).
        slides: Ordered list of slide specs. Each spec is ``{"layout": "<name>", ...layout-specific kwargs}``.
            Supported layouts: ``title``, ``bullets``, ``chart``, ``kpi_grid``,
            ``table``, ``two_column``, ``image``, ``text``. See module docstring
            for each layout's expected keys, or ``docs/PRESENTATIONS.md``.
        filename: Optional output filename (without or with ``.pptx`` suffix).
            Defaults to ``<title-slug>-<timestamp>-<rand>.pptx``.
        template_name: Optional template name (without or with ``.pptx`` suffix).
            Looked up under ``data/presentations/templates/``. Falls back to
            ``default.pptx`` if present, otherwise a blank deck.

    Returns:
        ``{"status": "success", "file_path": <path>, "slide_count": <n>, "template_used": <path|none>}``
        on success, or ``{"status": "error", "message": <reason>}`` on failure.
    """
    if not isinstance(slides, list) or not slides:
        return {
            "status": "error",
            "message": "slides must be a non-empty list of slide specs.",
        }

    # Validate every slide spec before we start writing — cheap upfront
    # catch instead of failing halfway through a 30-slide deck.
    for i, spec in enumerate(slides):
        if not isinstance(spec, dict):
            return {
                "status": "error",
                "message": f"slide {i} is not a dict: {type(spec).__name__}",
            }
        layout = spec.get("layout")
        if layout not in _KNOWN_LAYOUTS:
            return {
                "status": "error",
                "message": (
                    f"slide {i}: unknown layout {layout!r}. "
                    f"Known: {sorted(_KNOWN_LAYOUTS)}"
                ),
            }

    try:
        from pptx import Presentation
    except Exception as e:
        return {"status": "error", "message": f"python-pptx unavailable: {e}"}

    template_path = _resolve_template(template_name)
    try:
        prs = Presentation(template_path) if template_path else Presentation()
    except Exception as e:
        # Bad template file — surface the error rather than silently
        # falling back, so the user knows their template is broken.
        return {
            "status": "error",
            "message": (
                f"failed to open template {template_path!r}: {e}. "
                f"Move or fix the file, or pass template_name='' for a blank deck."
            ),
        }

    for i, spec in enumerate(slides):
        layout = spec["layout"]
        handler = _LAYOUT_DISPATCH[layout]
        try:
            handler(prs, spec)
        except Exception as e:
            return {
                "status": "error",
                "message": (
                    f"slide {i} ({layout}) failed during render: {e}. "
                    f"Spec keys: {sorted(spec.keys())}"
                ),
            }

    out_path = _output_path(filename, title)
    try:
        prs.save(out_path)
    except Exception as e:
        return {"status": "error", "message": f"failed to save deck: {e}"}

    logger.info(
        "generate_presentation: %d slides → %s (template=%s)",
        len(slides),
        out_path,
        template_path or "blank",
    )

    return {
        "status": "success",
        "file_path": out_path,
        "slide_count": len(slides),
        "template_used": template_path or "blank",
        "message": (
            f"Presentation '{title}' built with {len(slides)} slides "
            f"(template: {os.path.basename(template_path) if template_path else 'blank'}). "
            f"Attached to this response."
        ),
    }
