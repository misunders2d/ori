#!/usr/bin/env python3
"""V2 scheduler — phase scope guard.

Enforces docs/PHASE_1_PLAN.md §6 and docs/CONTRACTS_V2_DESIGN.md §12.1:

  1. Phase commits stay small.
  2. Phase 1 ≠ runtime behavior.
  3. Old scheduler stays untouched through phase 8 (boundary
     shifted from 7 with the 2026-05-15 §12 renumber that
     inserted "APScheduler binding + boot sequence" at step 5).

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
    5: {
        # Code surface for v2 binding / boot / lifecycle + tests
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
        "docs/PHASE_5_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Doc-read marker bookkeeping
        ".docs_read_marker",
    },
    6: {
        # Code surface for v2 registry cache + tests
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
        "docs/PHASE_6_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Doc-read marker bookkeeping
        ".docs_read_marker",
    },
    7: {
        # Code surface for v2 authoring tools + toolset + tests
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
        "docs/PHASE_7_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Doc-read marker bookkeeping
        ".docs_read_marker",
    },
    8: {
        # Code surface for v2 dry-run + freeze + commit + tests
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
        "docs/PHASE_8_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Doc-read marker bookkeeping
        ".docs_read_marker",
    },
    9: {
        # Code surface carried forward
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
        "docs/PHASE_9_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Auto-generated index — the pre-commit hook
        # regenerates + stages these whenever a structural
        # change (new module / tool / toolset) lands. v2
        # slices add modules, so the regenerated index must
        # be allowed in-scope; CLAUDE.md mandates the index
        # stays accurate after structural changes.
        "docs/INDEX.md",
        "docs/AGENTS_INVENTORY.md",
        # Doc-read marker bookkeeping
        ".docs_read_marker",
        # NEW for phase 9 — first cutover. CoordinatorAgent
        # mount + boot_runtime integration. v1 paths under
        # FORBIDDEN_PHASES_PRE_CUTOVER stay out of scope per the
        # plan even though the gate lifts at phase 9.
        "app/sub_agents/coordinator_agent.py",
        "run_bot.py",
        "app/agent.py",
    },
    10: {
        # Code surface carried forward
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
        "docs/PHASE_10_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Auto-generated index (pre-commit regenerates + stages)
        "docs/INDEX.md",
        "docs/AGENTS_INVENTORY.md",
        # Doc-read marker bookkeeping
        ".docs_read_marker",
        # NOT carried from phase 9: coordinator_agent.py /
        # run_bot.py / app/agent.py were phase-9 cutover-only.
        # Phase 10 (step 10 — source loaders + snapshot infra)
        # is a build-the-layer phase: no worker fire-path
        # wiring, no agent mount, no production side effect
        # before step 11 (§12.1 invariant 2).
    },
    11: {
        # Code surface carried forward
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
        "docs/PHASE_11_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Auto-generated index (pre-commit regenerates + stages)
        "docs/INDEX.md",
        "docs/AGENTS_INVENTORY.md",
        # Doc-read marker bookkeeping
        ".docs_read_marker",
        # RE-ADDED for phase 11 — step-11 fire-path cutover.
        # The phase-10 source layer goes live in the worker;
        # the coordinator §11.4 scheduling-law gains the new
        # RecurringSeriesFromSource template (ADDITIVE, like
        # phase 9). NOT carried from phase 10 (build-the-layer
        # had no agent mount). run_bot.py / app/agent.py are
        # NOT re-added: phase-9 already wired boot + slack DI;
        # phase 11 adds no new boot integration.
        "app/sub_agents/coordinator_agent.py",
    },
    12: {
        # Code surface carried forward
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
        "docs/PHASE_12_PLAN.md",
        "docs/CONTRACTS_V2_DESIGN.md",
        # Auto-generated index (pre-commit regenerates + stages)
        "docs/INDEX.md",
        "docs/AGENTS_INVENTORY.md",
        # Doc-read marker bookkeeping
        ".docs_read_marker",
        # NOT re-added for phase 12: coordinator_agent.py /
        # run_bot.py / app/agent.py. Step 12 is the
        # read-only-reasoning / emit-only-writes enforcement
        # layer (validation chokepoint + pure runtime guard) —
        # no new agent mount, no boot integration. Mirrors the
        # phase-10 build-the-layer allowlist (no agent surface).
    },
    # Future phases populate here as they land.
}

# Anything matching one of these prefixes / paths is BLOCKED for
# phases 1 through 8 ("pre-cutover"). Phase 9 (first end-to-end
# OneOffReminder) is the boundary at which v1 edits become
# permissible — see docs/CONTRACTS_V2_DESIGN.md §12.1 invariant 3.
# The boundary shifted from "1-through-7 / phase 8 cutover" to
# "1-through-8 / phase 9 cutover" with the 2026-05-15 §12 renumber.
# Constant renamed FORBIDDEN_PHASES_1_TO_8 →
# FORBIDDEN_PHASES_PRE_CUTOVER in the phase-9 round-2 revision
# to remove the post-renumber off-by-one confusion.
FORBIDDEN_PHASES_PRE_CUTOVER: set[str] = {
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
    phase is in the protected window (1..8)."""
    if phase >= 9:
        return False
    return any(_matches(path, entry) for entry in FORBIDDEN_PHASES_PRE_CUTOVER)


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
                f"{f} — forbidden in phases 1-8 (v1 scheduler path; "
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
