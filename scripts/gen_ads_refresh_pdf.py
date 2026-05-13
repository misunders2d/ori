"""Generate ads_refresh_token.pdf — one document, two audiences.

Section A: operator (Sergey) — what to run and when.
Section B: account owner — two browser-only steps.

The operator runs ``scripts/ads_oauth_helper.py --public`` which
imports and calls ``build(authorize_url)`` here so the PDF lands with
the authorize URL already inlined. Standalone invocation is also
supported for regenerating the PDF without running the helper:

    uv run --with reportlab python scripts/gen_ads_refresh_pdf.py
    uv run --with reportlab python scripts/gen_ads_refresh_pdf.py --url "<authorize URL>"
"""
from __future__ import annotations

import argparse
import os
import sys

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
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
        "section": ParagraphStyle(
            "section",
            parent=base["Heading1"],
            fontSize=15,
            spaceBefore=14,
            spaceAfter=10,
            textColor="#003366",
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontSize=13,
            spaceBefore=12,
            spaceAfter=6,
            textColor="#222222",
        ),
        "intro": ParagraphStyle(
            "intro",
            parent=base["BodyText"],
            fontSize=11,
            leading=15,
            spaceAfter=12,
        ),
        "step": ParagraphStyle(
            "step",
            parent=base["BodyText"],
            fontSize=11,
            leading=15,
            spaceAfter=8,
            leftIndent=14,
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
        "cmd": ParagraphStyle(
            "cmd",
            parent=base["Code"],
            fontSize=10,
            leading=13,
            backColor="#1e1e1e",
            textColor="#e0e0e0",
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
        Paragraph("Amazon Ads &mdash; one-time consent grant", s["title"])
    )
    flow.append(
        Paragraph(
            "Two parties, one short procedure. The bot runs on a VPS at "
            "<b>bezosapp.uk</b>; the account owner clicks Allow once in "
            "their browser; the bot writes the refresh token to its vault "
            "automatically. The refresh token never expires (unless the "
            "owner explicitly revokes it).",
            s["intro"],
        )
    )

    # ------------------------------------------------------------------
    # Section A — operator (Sergey)
    # ------------------------------------------------------------------
    flow.append(Paragraph("Section A &mdash; Operator (Sergey)", s["section"]))

    flow.append(Paragraph("A1. Deploy the new code to the VPS", s["h2"]))
    flow.append(Paragraph("SSH in, pull, restart the bot:", s["step"]))
    flow.append(
        Paragraph(
            "ssh &lt;vps&gt;<br/>"
            "cd &lt;ori repo&gt;<br/>"
            "git pull<br/>"
            "./deploy/start.sh",
            s["cmd"],
        )
    )
    flow.append(
        Paragraph(
            "Activates the <b>/oauth/ads/callback</b> route on "
            "<b>bezosapp.uk</b>.",
            s["step"],
        )
    )

    flow.append(Paragraph("A2. Mint the consent URL on the VPS", s["h2"]))
    flow.append(
        Paragraph(
            "uv run python scripts/ads_oauth_helper.py --public",
            s["cmd"],
        )
    )
    flow.append(
        Paragraph(
            "The helper prints an authorize URL and regenerates this PDF "
            "with the URL inlined into Section B Step 2 below. You ran "
            "this command to produce this very document.",
            s["step"],
        )
    )

    flow.append(Paragraph("A3. Send this PDF to the account owner", s["h2"]))
    flow.append(
        Paragraph(
            "Telegram / email / whichever channel. Owner follows Section B. "
            "They are non-technical &mdash; do not assume any terminal "
            "knowledge.",
            s["step"],
        )
    )

    flow.append(Paragraph("A4. Wait", s["h2"]))
    flow.append(
        Paragraph(
            "When the owner finishes Section B, the bot's callback handler "
            "swaps the authorization code with Amazon and writes "
            "<b>ADS_API_REFRESH_TOKEN</b> to the vault. Watch bot logs for:",
            s["step"],
        )
    )
    flow.append(
        Paragraph(
            "ADS_API_REFRESH_TOKEN written to vault via OAuth callback.",
            s["cmd"],
        )
    )

    flow.append(Spacer(1, 0.15 * inch))
    flow.append(HRFlowable(width="100%", thickness=0.5, color="#888888"))
    flow.append(Spacer(1, 0.15 * inch))

    # ------------------------------------------------------------------
    # Section B — account owner
    # ------------------------------------------------------------------
    flow.append(Paragraph("Section B &mdash; Account owner", s["section"]))
    flow.append(
        Paragraph(
            "Two browser-only actions, about 60 seconds total. No terminal, "
            "no copy-paste from error pages.",
            s["intro"],
        )
    )

    flow.append(Paragraph("B1. Allow the redirect URL (one time only)", s["h2"]))
    flow.append(
        Paragraph(
            "Open this page in your browser and sign in with the Amazon "
            "developer account that owns the Login-with-Amazon application "
            "for the Ads integration:",
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
    flow.append(Paragraph("1d. Click <b>Save</b>.", s["step"]))

    flow.append(Paragraph("B2. Grant the consent", s["h2"]))
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
            "2a. Amazon will show a consent screen describing what data "
            "the application will be allowed to access. Click <b>Allow</b>.",
            s["step"],
        )
    )
    flow.append(
        Paragraph(
            "2b. The page will show a short <b>Connected!</b> message. "
            "That confirms the grant landed. You can close the tab.",
            s["step"],
        )
    )

    flow.append(Spacer(1, 0.2 * inch))
    flow.append(
        Paragraph(
            "If anything goes wrong &mdash; the wrong Amazon account is "
            "signed in, the consent screen never appears, or you see an "
            "error instead of the Connected! page &mdash; message Sergey. "
            "He can issue a fresh link and you can try again.",
            s["intro"],
        )
    )

    doc.build(flow)
    abs_path = os.path.abspath(OUTPUT_FILE)
    print(f"Wrote {abs_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the operator + owner consent PDF."
    )
    parser.add_argument(
        "--url",
        default="<PASTE AUTHORIZE URL HERE>",
        help=(
            "The authorize URL printed by `ads_oauth_helper.py --public`. "
            "If omitted, a placeholder is used and the operator must "
            "regenerate the PDF before sending."
        ),
    )
    args = parser.parse_args()
    build(args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
