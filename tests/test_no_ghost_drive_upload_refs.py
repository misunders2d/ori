"""Static guard: no source file may claim that ``drive_upload_file``
is a real, callable tool in this build.

Triggered by the 2026-05-20 cron_97f22322 incident. The cron's
prompt step 6 said "upload the CSV to Google Drive," and the
LLM's instruction surface (via docstrings + skill + design doc)
told it `drive_upload_file` was a callable separate tool — but
``GoogleWorkspaceToolset.get_tools`` never registered such a
primitive. Result: the LLM tried, got nothing, and fabricated
"Drive upload was bypassed as the account is not connected" as
a cover story.

This test scans the three sites that historically asserted the
tool's existence and confirms each one now explicitly says the
tool does NOT exist. Future PRs that drop the "does NOT exist"
qualifier without adding the actual tool will fail this test.

It does NOT check that ``drive_upload_file`` is genuinely
unregistered — slice 6b's instruction validator (planned) covers
that runtime invariant. This test pins the documentation surface.
"""

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


SITES = (
    REPO_ROOT / "app" / "tools" / "presentations.py",
    REPO_ROOT / "docs" / "PRESENTATIONS.md",
    REPO_ROOT / "skills" / "presentation-skill" / "SKILL.md",
)


@pytest.mark.parametrize("path", SITES)
def test_site_exists(path):
    assert path.exists(), f"expected site at {path}"


@pytest.mark.parametrize("path", SITES)
def test_site_mentions_drive_upload_only_with_does_not_exist_caveat(path):
    """If the file mentions ``drive_upload_file`` at all, the same
    file must somewhere also say the tool does NOT exist / no
    primitive is available / etc.

    Strict invariant: any positive claim like 'the existing
    drive_upload_file tool' or 'call drive_upload_file' without
    the surrounding "does NOT exist" disclaimer is a regression
    of the v3 cleanup."""
    text = path.read_text(encoding="utf-8")
    if "drive_upload_file" not in text:
        # Trivially safe — file makes no claim.
        return
    lower = text.lower()
    disclaimer_phrases = (
        "does not exist",
        "no drive-upload primitive",
        "no `drive_upload_file` primitive",
        "no drive-upload tool",
        "do not fabricate",
        "do not invent a tool",
        "no satisfying primitive",
    )
    assert any(p in lower for p in disclaimer_phrases), (
        f"{path} mentions `drive_upload_file` without any of the "
        f"approved disclaimer phrases. Either add the actual tool to "
        f"GoogleWorkspaceToolset or strip the reference."
    )


def test_no_positive_call_phrasing_remains():
    """Forbidden positive phrasings — these are imperative or
    declarative claims that ``drive_upload_file`` is a callable tool.
    Matched by regex so backticked / unbackticked / single-backtick /
    double-backtick variants all collide.

    Patterns intentionally narrow: only flag wordings that direct the
    LLM to USE the tool (call, invoke, use, run, execute) or
    declarative existence claims (the existing X, X tool is, the X
    tool, call X separately). Disclaimer wordings ("no
    `drive_upload_file` primitive", "`drive_upload_file` does NOT
    exist") stay legal — those are what slice 6a's strip preserves.

    Reviewer feedback v5→v6 of slice 6a: previously matched only the
    exact pre-strip strings. A future PR that re-introduces the ghost
    with slightly different phrasing (e.g. "use drive_upload_file",
    "invoke `drive_upload_file`") would have slipped through.
    """
    import re as _re

    # All these wrappers — ``foo``, `foo`, foo — collapse to the same
    # pattern matched as `[`*]?{name}[`*]?` (greedy backticks/stars
    # allowed at both edges).
    name = r"`*\*?drive_upload_file\*?`*"

    forbidden_patterns = [
        # Imperative: "call drive_upload_file", "use the
        # drive_upload_file tool", "invoke `drive_upload_file`", etc.
        _re.compile(
            rf"\b(?:call(?:s|ing|ed)?|invok(?:e|ing|es|ed)?|us(?:e|ing|es|ed)?|"
            rf"run(?:s|ning)?|execut(?:e|ing|es|ed)?|trigger(?:s|ing|ed)?)\b"
            rf"[^.\n]{{0,40}}\b{name}\b",
            _re.IGNORECASE,
        ),
        # Declarative existence: "the existing drive_upload_file",
        # "the drive_upload_file tool", "the existing `drive_upload_file` tool".
        _re.compile(
            rf"\bthe\s+(?:existing\s+)?{name}(?:\s+tool)?\b",
            _re.IGNORECASE,
        ),
        # "drive_upload_file tool is reachable" / "is available" /
        # "is wired" — declarative wiring claim.
        _re.compile(
            rf"\b{name}\b[^.\n]{{0,40}}\b(?:is\s+(?:reachable|available|wired|"
            rf"registered|exposed|callable))\b",
            _re.IGNORECASE,
        ),
        # "{name} separately" / "{name} afterwards" — the historical
        # bridging phrase that primed the v3 incident.
        _re.compile(
            rf"\b{name}\s+(?:separately|afterwards|after|next)\b",
            _re.IGNORECASE,
        ),
    ]

    for path in SITES:
        text = path.read_text(encoding="utf-8")
        for pat in forbidden_patterns:
            m = pat.search(text)
            assert m is None, (
                f"{path} contains forbidden positive phrasing "
                f"matched by {pat.pattern!r}: {m.group(0)!r}. "
                f"Either drop the reference or rewrite with the "
                f"'this tool does NOT exist' disclaimer per slice 6a "
                f"of the cron_97f22322 work."
            )
