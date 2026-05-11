#!/usr/bin/env python3
"""Doc-read marker writer for the pre-commit gate.

Every commit must be preceded by reading:
- `docs/AI_EDITS.md` — the 12 rules
- `docs/INDEX.md`    — the auto-generated symbol map

This script verifies both files exist, prompts (or auto-checks) that
the caller has actually read them, and writes `.docs_read_marker`
which `.githooks/pre-commit` looks for.

Modes:
- Default: interactive. Prints the required reading list, asks for
  explicit acknowledgement, then writes the marker.
- `--non-interactive`: skip the prompt; for CI / automated pipelines
  where someone else (a senior reviewer) has vouched for the change.
- `--show`: print marker status (age, presence) and exit.

The marker is intentionally short-lived (2 h, enforced by the hook) so
agents that walk away mid-session don't carry over stale acknowledgements.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MARKER_PATH = REPO_ROOT / ".docs_read_marker"
REQUIRED_DOCS = ("docs/AI_EDITS.md", "docs/INDEX.md")
MARKER_TTL_SECONDS = 2 * 3600


def _print(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def _check_docs_exist() -> list[str]:
    missing = [d for d in REQUIRED_DOCS if not (REPO_ROOT / d).is_file()]
    return missing


def _marker_status() -> tuple[bool, int]:
    """Return (fresh, age_seconds). fresh=False when missing or stale."""
    if not MARKER_PATH.is_file():
        return False, -1
    age = int(time.time() - MARKER_PATH.stat().st_mtime)
    return age <= MARKER_TTL_SECONDS, age


def _write_marker(reason: str) -> None:
    MARKER_PATH.write_text(
        f"# Doc-read marker — written {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f"# Acknowledges that the committer has read:\n"
        + "\n".join(f"#   - {d}" for d in REQUIRED_DOCS)
        + f"\n# Reason: {reason}\n"
    )


def _confirm_interactive() -> bool:
    _print()
    _print("📚 Required reading before commits:")
    for doc in REQUIRED_DOCS:
        _print(f"   - {doc}")
    _print()
    _print("Have you opened both files in this session and reviewed any sections")
    _print("relevant to the change you're about to commit?")
    try:
        reply = input("Type 'yes' to acknowledge: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return reply in ("y", "yes")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Doc-read marker writer.")
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Skip prompt; write marker without asking.",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Print marker status (presence + age) and exit.",
    )
    args = parser.parse_args(argv)

    if args.show:
        fresh, age = _marker_status()
        if age < 0:
            _print("Marker: absent")
        else:
            _print(f"Marker: {'fresh' if fresh else 'stale'} (age {age}s, limit {MARKER_TTL_SECONDS}s)")
        return 0 if fresh else 1

    missing = _check_docs_exist()
    if missing:
        _print(f"ERROR: required docs missing from repo: {', '.join(missing)}")
        _print("Run `uv run python scripts/gen_docs.py` to regenerate the auto-built ones.")
        return 2

    if not args.non_interactive:
        if not _confirm_interactive():
            _print("Aborted — marker not written.")
            return 1
        reason = "interactive acknowledgement"
    else:
        reason = "non-interactive (CI / automated)"

    _write_marker(reason)
    _print(f"✓ Marker written: {MARKER_PATH.relative_to(REPO_ROOT)} (valid {MARKER_TTL_SECONDS}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
