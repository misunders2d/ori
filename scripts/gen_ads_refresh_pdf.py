"""Generate ads_refresh_token.pdf — owner-facing consent instructions.

The recipient is the owner of an Amazon advertising account who also
owns the Amazon developer account (so they can edit Allowed Return
URLs in the Login-with-Amazon console). They are NOT technical — no
laptop, no terminal, no copy-pasting URLs from error pages. All
interactions are point-and-click in a browser.

Run:

    uv run --with reportlab python scripts/gen_ads_refresh_pdf.py
    uv run --with reportlab python scripts/gen_ads_refresh_pdf.py --url "<authorize URL>"

The ``--url`` flag inlines the authorize URL into Step 4 so the doc is
self-contained. Without it the PDF shows ``<PASTE AUTHORIZE URL HERE>``
as a placeholder that the operator fills in before sending.
"""
from __future__ import annotations

import argparse
import os
import sys

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)


OUTPUT_FILE = "ads_refresh_token.pdf"
CALLBACK_URL = "https://bezosapp.uk/oauth/ads/callback"


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title",
            parent=base["Title"],
            fontSize=18,
            spaceAfter=12,
            leading=22,
        ),
        "intro": ParagraphStyle(
            "intro",
            parent=base["BodyText"],
            fontSize=11,
            leading=15,
            spaceAfter=12,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontSize=13,
            spaceBefore=14,
            spaceAfter=6,
            textColor="#222222",
        ),
        "step": ParagraphStyle(
            "step",
            parent=base["BodyText"],
            fontSize=11,
            leading=15,
            spaceAfter=8,
            leftIndent=14,
        ),
        "warn": ParagraphStyle(
            "warn",
            parent=base["BodyText"],
            fontSize=11,
            leading=15,
            spaceAfter=8,
            leftIndent=14,
            textColor="#a02020",
        ),
        "url": ParagraphStyle(
            "url",
            parent=base["Code"],
            fontSize=10,
            leading=13,
            backColor="#f0f0f0",
            spaceAfter=10,
            leftIndent=14,
            rightIndent=14,
            borderPadding=6,
        ),
    }


def build(authorize_url: str) -> None:
    doc = SimpleDocTemplate(
        OUTPUT_FILE,
        pagesize=LETTER,
        leftMargin=0.8 * inch,
        rightMargin=0.8 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        title="Amazon Ads — one-time consent grant",
        author="Mellanni",
    )
    s = _styles()
    flow = []

    flow.append(
        Paragraph(
            "Amazon Ads &mdash; one-time consent grant",
            s["title"],
        )
    )
    flow.append(
        Paragraph(
            "We need a quick one-time approval on the advertising account so "
            "our automation can pull reports. Two short tasks, all in the "
            "browser. About 60 seconds total.",
            s["intro"],
        )
    )

    # ------------------------------------------------------------------
    # Step 1 — register the callback URL in the Login-with-Amazon console.
    # Owner is the developer-account holder, so they can edit this.
    # ------------------------------------------------------------------
    flow.append(Paragraph("Step 1 &mdash; allow the redirect URL (one time only)", s["h2"]))
    flow.append(
        Paragraph(
            "Open this page in your browser and sign in with the Amazon "
            "developer account that owns the Login-with-Amazon application "
            "for our Ads integration:",
            s["step"],
        )
    )
    flow.append(
        Paragraph(
            "https://developer.amazon.com/loginwithamazon/console/site/lwa/overview.html",
            s["url"],
        )
    )
    flow.append(
        Paragraph(
            "1a. You will see a list of Login with Amazon applications. "
            "Click the one we use for the Amazon Ads integration.",
            s["step"],
        )
    )
    flow.append(
        Paragraph(
            "1b. Open the <b>Web Settings</b> tab and click <b>Edit</b>.",
            s["step"],
        )
    )
    flow.append(
        Paragraph(
            "1c. In the <b>Allowed Return URLs</b> field, add this value on "
            "its own line, exactly as written (no extra spaces):",
            s["step"],
        )
    )
    flow.append(Paragraph(CALLBACK_URL, s["url"]))
    flow.append(
        Paragraph(
            "1d. Click <b>Save</b>.",
            s["step"],
        )
    )

    # ------------------------------------------------------------------
    # Step 2 — actually grant consent. Browser-only, ends on a success page.
    # ------------------------------------------------------------------
    flow.append(Paragraph("Step 2 &mdash; grant the consent", s["h2"]))
    flow.append(
        Paragraph(
            "Open the link below in the same browser (the one signed in to "
            "the Amazon account that owns the advertising account):",
            s["step"],
        )
    )
    flow.append(Paragraph(authorize_url, s["url"]))
    flow.append(
        Paragraph(
            "2a. Amazon will show a consent screen describing what data the "
            "application will be allowed to access. Click <b>Allow</b>.",
            s["step"],
        )
    )
    flow.append(
        Paragraph(
            "2b. The page will then show a short <b>Connected!</b> message. "
            "That confirms the grant landed. You can close the tab.",
            s["step"],
        )
    )

    flow.append(Spacer(1, 0.2 * inch))
    flow.append(
        Paragraph(
            "If anything goes wrong &mdash; you sign in to the wrong account, "
            "the consent screen never appears, or Amazon shows an error "
            "instead of the Connected! page &mdash; just message Sergey. He "
            "can send a fresh link and you can try again.",
            s["intro"],
        )
    )

    doc.build(flow)
    abs_path = os.path.abspath(OUTPUT_FILE)
    print(f"Wrote {abs_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the owner-facing consent PDF."
    )
    parser.add_argument(
        "--url",
        default="<PASTE AUTHORIZE URL HERE>",
        help=(
            "The authorize URL printed by `ads_oauth_helper.py --public`. "
            "If omitted, a placeholder is used and the operator must edit "
            "the PDF manually before sending."
        ),
    )
    args = parser.parse_args()
    build(args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
