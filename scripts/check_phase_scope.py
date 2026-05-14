#!/usr/bin/env python3
"""V2 scheduler — phase scope guard.

Enforces docs/PHASE_1_PLAN.md §6 and docs/CONTRACTS_V2_DESIGN.md §12.1:

  1. Phase commits stay small.
  2. Phase 1 ≠ runtime behavior.
  3. Old scheduler stays untouched through phase 7.

Run modes:

  * ``--staged``: compares the index against HEAD (pre-commit hook).
  * ``--diff <ref>``: compares HEAD against the given ref (CI workflow,
    usually ``--diff origin/master`` or similar).

Exits 0 on clean; 1 on violation. Override via
``PHASE_OVERRIDE: <reason>`` in the commit message body — local hook
still rejects but CI flags ``requires_human_ack`` (see
docs/PHASE_1_PLAN.md §6.4). This script does NOT read commit messages;
the local pre-commit hook checks the violations; override handling is
the CI workflow's job.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys


# ---------------------------------------------------------------------------
# Allowlist + forbidden paths — see docs/PHASE_1_PLAN.md §6.3
# ---------------------------------------------------------------------------

PHASE_ALLOWLIST: dict[int, set[str]] = {
    1: {
        # Code surface for v2 schemas / migrations / tests
        "app/v2/",
        "tests/v2/",
        # CI guard machinery
        "scripts/check_phase_scope.py",
        "scripts/install_hooks.py",
        ".githooks/v2_phase_guard.sh",
        ".githooks/pre-commit",
        ".github/workflows/v2_phase_guard.yml",
        # Phase-tracking artefact
        ".v2-current-phase",
        # Plan + design docs
        "docs/PHASE_1_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Doc-read marker bookkeeping (existing pre-commit gate)
        ".docs_read_marker",
    },
    2: {
        # Code surface for v2 adapter/source contracts + tests
        "app/v2/",
        "tests/v2/",
        # CI guard machinery (carried forward from phase 1)
        "scripts/check_phase_scope.py",
        "scripts/install_hooks.py",
        ".githooks/v2_phase_guard.sh",
        ".githooks/pre-commit",
        ".github/workflows/v2_phase_guard.yml",
        # Phase-tracking artefact
        ".v2-current-phase",
        # Plan + design docs
        "docs/PHASE_2_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Doc-read marker bookkeeping
        ".docs_read_marker",
    },
    3: {
        # Code surface for v2 storage CRUD layer + tests
        "app/v2/",
        "tests/v2/",
        # CI guard machinery (carried forward)
        "scripts/check_phase_scope.py",
        "scripts/install_hooks.py",
        ".githooks/v2_phase_guard.sh",
        ".githooks/pre-commit",
        ".github/workflows/v2_phase_guard.yml",
        # Phase-tracking artefact
        ".v2-current-phase",
        # Plan + design docs
        "docs/PHASE_3_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Doc-read marker bookkeeping
        ".docs_read_marker",
    },
    4: {
        # Code surface for v2 worker / claim / wakeup + tests
        "app/v2/",
        "tests/v2/",
        # CI guard machinery (carried forward)
        "scripts/check_phase_scope.py",
        "scripts/install_hooks.py",
        ".githooks/v2_phase_guard.sh",
        ".githooks/pre-commit",
        ".github/workflows/v2_phase_guard.yml",
        # Phase-tracking artefact
        ".v2-current-phase",
        # Plan + design docs
        "docs/PHASE_4_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Doc-read marker bookkeeping
        ".docs_read_marker",
    },
    # Future phases populate here as they land.
}

# Anything matching one of these prefixes / paths is BLOCKED for
# phases 1 through 7. Phase 8 (first end-to-end OneOffReminder) is
# the boundary at which v1 edits become permissible — see
# docs/CONTRACTS_V2_DESIGN.md §12.1 invariant 3.
FORBIDDEN_PHASES_1_TO_7: set[str] = {
    "app/contracts/",
    "app/tasks.py",
    "app/contracts/executor.py",
    "app/scheduler_instance.py",
    "data/contracts/",
}


# ---------------------------------------------------------------------------
# Path matching helpers
# ---------------------------------------------------------------------------


def _matches(path: str, pattern: str) -> bool:
    """Path-prefix match.

    Pattern ending in ``/`` matches any path starting with the prefix
    (directory match). Otherwise exact-path match.
    """
    if pattern.endswith("/"):
        return path.startswith(pattern) or path == pattern.rstrip("/")
    return path == pattern


def is_allowed(path: str, phase: int) -> bool:
    """Return True iff ``path`` is in the phase's allowlist."""
    allowed = PHASE_ALLOWLIST.get(phase, set())
    return any(_matches(path, entry) for entry in allowed)


def is_forbidden(path: str, phase: int) -> bool:
    """Return True iff ``path`` matches a forbidden v1 path AND the
    phase is in the protected window (1..7)."""
    if phase >= 8:
        return False
    return any(_matches(path, entry) for entry in FORBIDDEN_PHASES_1_TO_7)


# ---------------------------------------------------------------------------
# Git plumbing
# ---------------------------------------------------------------------------


def current_phase() -> int | None:
    """Read ``.v2-current-phase`` (single integer). Returns None when
    the file is missing — guard is then inert."""
    p = pathlib.Path(".v2-current-phase")
    if not p.exists():
        return None
    text = p.read_text().strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _git(*args: str) -> str:
    """Run a git command, returning stdout. Raises CalledProcessError
    on non-zero exit."""
    res = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


def get_staged_files() -> list[str]:
    """Files staged for the next commit (ACMRT only — drops D/X/U)."""
    out = _git(
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMRT",
    ).strip()
    return [line for line in out.split("\n") if line]


def get_diff_files(base_ref: str) -> list[str]:
    """Files changed between ``base_ref`` and HEAD.

    Reports EVERY touched path regardless of whether a commit
    in the range carries a ``PHASE_OVERRIDE:`` marker.

    Override semantics live in
    ``.github/workflows/v2_phase_guard.yml``: when this script
    exits non-zero AND a ``PHASE_OVERRIDE:`` line appears in the
    range's commit messages, the workflow labels the PR
    ``requires_human_ack`` and blocks merge until an admin
    explicitly approves. Per docs/PHASE_1_PLAN.md §6.4 the
    override never makes a violation disappear — it only changes
    the failure mode from "hard fail" to "require human ack".

    Filtering override files out of the violation set HERE
    would short-circuit that path: the script would exit 0, the
    workflow would skip the labeling step, and the documented
    ack requirement would silently be bypassed. So the helper
    stays purely path-based.
    """
    out = _git(
        "diff",
        "--name-only",
        f"{base_ref}...HEAD",
    ).strip()
    return [line for line in out.split("\n") if line]


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def evaluate(files: list[str], phase: int) -> list[str]:
    """Return a list of human-readable violation strings. Empty list
    means the diff is clean for the given phase."""
    violations: list[str] = []
    for f in files:
        if is_forbidden(f, phase):
            violations.append(
                f"{f} — forbidden in phases 1-7 (v1 scheduler path; "
                "see docs/CONTRACTS_V2_DESIGN.md §12.1 invariant 3)"
            )
            continue
        if not is_allowed(f, phase):
            violations.append(
                f"{f} — not in phase {phase} allowlist "
                "(see docs/PHASE_1_PLAN.md §6.3)"
            )
    return violations


def _emit_report(violations: list[str], phase: int) -> None:
    print(f"❌  PHASE {phase} SCOPE VIOLATION", file=sys.stderr)
    print("", file=sys.stderr)
    for v in violations:
        print(f"   {v}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "If this is intentional (e.g. emergency v1 hotfix during a v2 "
        "phase), add a 'PHASE_OVERRIDE: <reason>' line to the commit "
        "message body. The local hook still rejects, but CI honours the "
        "override with required human ack — see docs/PHASE_1_PLAN.md §6.4.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="V2 scheduler phase scope guard.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--staged",
        action="store_true",
        help="check files staged for the next commit (pre-commit hook).",
    )
    mode.add_argument(
        "--diff",
        metavar="BASE_REF",
        help="check files changed between BASE_REF and HEAD (CI workflow).",
    )
    parser.add_argument(
        "--phase",
        type=int,
        default=None,
        help="override phase number (mainly for testing). Defaults to "
        "reading .v2-current-phase.",
    )
    args = parser.parse_args(argv)

    phase = args.phase if args.phase is not None else current_phase()
    if phase is None:
        # Guard is inert; nothing to do.
        return 0

    try:
        if args.staged:
            files = get_staged_files()
        else:
            files = get_diff_files(args.diff)
    except subprocess.CalledProcessError as e:
        print(
            f"phase guard: git invocation failed ({e}). Skipping.",
            file=sys.stderr,
        )
        return 0

    if not files:
        return 0

    violations = evaluate(files, phase)
    if violations:
        _emit_report(violations, phase)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
