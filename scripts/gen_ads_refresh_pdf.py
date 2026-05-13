"""Generate ads_refresh_token.pdf — static operator runbook.

This is Sergey's reference doc for the Amazon Ads consent grant. It
does NOT carry the dynamic authorize URL (that token has a single-use
state nonce and is minted fresh every time `ads_oauth_helper.py
--public` runs). Keep this PDF on disk; re-read whenever a new
account owner needs to be onboarded.

Run:

    uv run --with reportlab python scripts/gen_ads_refresh_pdf.py
"""
from __future__ import annotations

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


def build() -> None:
    doc = SimpleDocTemplate(
        OUTPUT_FILE,
        pagesize=LETTER,
        leftMargin=0.8 * inch,
        rightMargin=0.8 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
        title="Amazon Ads — consent runbook",
        author="Mellanni",
    )
    s = _styles()
    flow = []

    flow.append(Paragraph("Amazon Ads &mdash; consent runbook", s["title"]))
    flow.append(
        Paragraph(
            "Reference for granting one-time API consent on an Amazon Ads "
            "advertising account. Two roles: the operator (Sergey) and the "
            "account owner. Refresh tokens are permanent until the owner "
            "explicitly revokes them, so the procedure runs once per account.",
            s["intro"],
        )
    )

    # -------------------------------------------------------------
    # Section A — operator (Sergey)
    # -------------------------------------------------------------
    flow.append(Paragraph("Section A &mdash; Operator (Sergey)", s["section"]))

    flow.append(Paragraph("A1. Deploy current code to the VPS", s["h2"]))
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
            "Activates <b>/oauth/ads/callback</b> on <b>bezosapp.uk</b>.",
            s["step"],
        )
    )

    flow.append(Paragraph("A2. Mint the consent URL", s["h2"]))
    flow.append(
        Paragraph(
            "uv run python scripts/ads_oauth_helper.py --public",
            s["cmd"],
        )
    )
    flow.append(
        Paragraph(
            "Prints an authorize URL (HMAC-signed state, single-use) and the "
            "redirect URI to the terminal. Copy the URL.",
            s["step"],
        )
    )

    flow.append(Paragraph("A3. Send a short message to the account owner", s["h2"]))
    flow.append(
        Paragraph(
            "Telegram / email. Two things in the message: the redirect URI "
            "they must whitelist, and the authorize URL they click. Section "
            "B below is what they have to do on their end.",
            s["step"],
        )
    )

    flow.append(Paragraph("A4. Watch the bot logs", s["h2"]))
    flow.append(
        Paragraph(
            "When the owner completes Section B, the callback handler swaps "
            "the code with Amazon and writes the refresh token. Look for:",
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

    # -------------------------------------------------------------
    # Section B — account owner
    # -------------------------------------------------------------
    flow.append(Paragraph("Section B &mdash; Account owner (what they do)", s["section"]))
    flow.append(
        Paragraph(
            "Two browser-only actions, about 60 seconds total. No terminal.",
            s["intro"],
        )
    )

    flow.append(Paragraph("B1. Allow the redirect URL (one time only)", s["h2"]))
    flow.append(
        Paragraph(
            "Open this page and sign in with the Amazon developer account "
            "that owns the Login-with-Amazon application for the Ads "
            "integration:",
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
            "Click the Login-with-Amazon app for the Ads integration &rarr; "
            "<b>Web Settings</b> tab &rarr; <b>Edit</b> &rarr; add this "
            "value to <b>Allowed Return URLs</b> on its own line, exactly "
            "as written:",
            s["step"],
        )
    )
    flow.append(Paragraph(CALLBACK_URL, s["url"]))
    flow.append(Paragraph("Click <b>Save</b>.", s["step"]))

    flow.append(Paragraph("B2. Grant the consent", s["h2"]))
    flow.append(
        Paragraph(
            "Open the authorize URL Sergey sent (in the same browser, signed "
            "in to the Amazon account that owns the advertising account). "
            "Click <b>Allow</b> on the consent screen. The next page shows "
            "<b>Connected!</b> &mdash; done. Close the tab.",
            s["step"],
        )
    )

    flow.append(Spacer(1, 0.2 * inch))
    flow.append(
        Paragraph(
            "If anything goes wrong &mdash; wrong Amazon account signed in, "
            "consent screen never appears, error page instead of "
            "Connected! &mdash; message Sergey. He issues a fresh URL and "
            "they retry.",
            s["intro"],
        )
    )

    doc.build(flow)
    abs_path = os.path.abspath(OUTPUT_FILE)
    print(f"Wrote {abs_path}")


def main() -> int:
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
