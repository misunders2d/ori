#!/usr/bin/env python3
"""Pre-push preflight for the scheduling-fixes + telegram-pair work.

Run from the repo root before pushing ``evo/amazon_manager``:

    uv run python scripts/check_preflight.py            # default (lenient)
    uv run python scripts/check_preflight.py --strict   # fail on info-only findings

Sections (in order):

1. **Boot validator dry-run** — builds the live ``app.agent.root_agent``
   + ``app.scheduler_instance.scheduler``, runs the slice-6b
   ``validate_agent_tool_refs`` + ``audit_persisted_jobs`` scans.
   Ghost-tool findings or risky-cron audit hits = ``[FAIL]``
   regardless of strict mode (these are the load-bearing
   regressions the new validator was built to catch).

2. **Per-job trigger audit** — enumerates every persisted
   APScheduler job and dry-runs ``app.tasks._match_triggers`` on
   each ``task_prompt`` so the operator can spot crons that the
   new fabrication detection might newly fail-the-task. Informative
   by default; ``[FAIL]`` only when ``--strict`` is passed.

3. **Full pytest** — ``uv run python -m pytest tests/ -q``. Any
   failure = ``[FAIL]``.

4. **Working-tree snapshot** — warns on dirty ``app/*`` / ``docs/*``
   paths (out-of-scope drift can break the push). Untracked files
   are listed but never fail.

5. **Recent-commits dry-run** — ``git log --oneline
   origin/evo/amazon_manager..HEAD``. Lists subjects; warns if the
   delta looks unexpectedly large or empty.

Each section emits one of ``[OK]`` / ``[WARN]`` / ``[FAIL]`` and a
short summary. A final block summarises across all sections. Exit
code is non-zero iff any section ended ``[FAIL]``.

The script must NEVER push, fetch, or mutate git state. Pre-commit
hooks are not invoked.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from typing import Any

# Status sentinels.
OK = "[OK]"
WARN = "[WARN]"
FAIL = "[FAIL]"

# Reasonable bounds for the ahead-of-origin commit count. Outside
# this window we WARN (not FAIL) so the operator pauses to think.
_AHEAD_LO = 5
_AHEAD_HI = 30


# ---------------------------------------------------------------------------
# Persisted-job loader
#
# APScheduler's ``Scheduler.get_jobs()`` only returns jobs the scheduler
# can dispatch at this moment — for a STOPPED scheduler that's the
# pending in-memory queue ([] in practice for a freshly-constructed
# AsyncIOScheduler). Persisted rows in the SQLAlchemyJobStore are
# invisible until the scheduler starts. That makes
# ``scheduler.get_jobs()`` useless for a pre-push audit.
#
# This preflight reads the SQLAlchemyJobStore directly via its public
# ``get_all_jobs()`` method (see
# ``.venv/.../apscheduler/jobstores/sqlalchemy.py:108``). The
# jobstore's engine + Table are constructed at __init__, so no
# scheduler lifecycle is required. We still call ``start(scheduler,
# "default")`` (idempotent — uses ``Table.create(checkfirst=True)``)
# so a fresh dev clone with no jobstore table doesn't blow up.
# ---------------------------------------------------------------------------


class _SchedulerShim:
    """Minimal duck-typed scheduler that just yields a fixed list of
    jobs. Lets us pass persisted-jobstore output through
    ``audit_persisted_jobs(scheduler).get_jobs()`` without changing
    the production audit signature."""

    def __init__(self, jobs: list[Any]) -> None:
        self._jobs = list(jobs)

    def get_jobs(self) -> list[Any]:
        return self._jobs


def _read_persisted_jobs(scheduler: Any) -> list[Any] | None:
    """Return every persisted job from the scheduler's default
    SQLAlchemyJobStore — read directly, no scheduler start.

    Returns ``None`` (NOT an empty list) when the jobstore can't be
    introspected. Callers WARN on ``None`` so the operator knows
    persisted jobs were skipped (different signal from "scheduler
    has no jobs").
    """
    try:
        stores = getattr(scheduler, "_jobstores", None)
        if not stores:
            return None
        jobstore = stores.get("default")
        if jobstore is None:
            return None
        # Idempotent: ensure the underlying SQL table exists. APScheduler's
        # ``start`` for SQLAlchemyJobStore calls Table.create with
        # ``checkfirst=True`` so this is safe on already-populated
        # production DBs and on clean dev clones alike.
        start = getattr(jobstore, "start", None)
        if callable(start):
            try:
                start(scheduler, "default")
            except Exception:
                # Already-started jobstore raises in some APScheduler
                # versions; ignore and try get_all_jobs anyway.
                pass
        get_all = getattr(jobstore, "get_all_jobs", None)
        if not callable(get_all):
            return None
        return list(get_all())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


async def section_boot_validator() -> tuple[str, str]:
    """Section 1. Returns (status, summary_text)."""
    try:
        from app.agent import root_agent
        from app.core.instruction_validator import (
            audit_persisted_jobs,
            validate_agent_tool_refs,
        )
        from app.scheduler_instance import scheduler
    except Exception as exc:
        return FAIL, f"import failed: {type(exc).__name__}: {exc}"

    try:
        findings = await validate_agent_tool_refs(root_agent)
    except Exception as exc:
        return FAIL, f"validate_agent_tool_refs raised: {type(exc).__name__}: {exc}"

    # Read persisted jobs directly from the jobstore; ``scheduler.get_jobs()``
    # would be empty on a stopped scheduler (see _read_persisted_jobs).
    persisted = _read_persisted_jobs(scheduler)
    jobs_unavailable = persisted is None
    audit_scheduler: Any
    if jobs_unavailable:
        audit_scheduler = _SchedulerShim([])
    else:
        audit_scheduler = _SchedulerShim(persisted)

    try:
        audit = audit_persisted_jobs(audit_scheduler)
    except Exception as exc:
        return FAIL, f"audit_persisted_jobs raised: {type(exc).__name__}: {exc}"

    if findings:
        lines = ["ghost-tool findings:"]
        for f in findings:
            lines.append(
                f"  agent=`{f.get('agent')}` source=`{f.get('source')}` "
                f"unresolved={f.get('unresolved')}"
            )
        if audit:
            lines.append(f"risky-cron audit ({len(audit)}):")
            for entry in audit:
                lines.append(
                    f"  job={entry.get('job_id')} matched={entry.get('matched')}"
                )
        return FAIL, "\n".join(lines)

    if audit:
        lines = [f"risky-cron audit ({len(audit)}):"]
        for entry in audit:
            lines.append(
                f"  job={entry.get('job_id')} matched={entry.get('matched')} "
                f"kinds={entry.get('kinds')}"
            )
        return FAIL, "\n".join(lines)

    if jobs_unavailable:
        # Audit ran on an empty list because the jobstore could not be
        # introspected. Don't claim "clean" — surface a WARN so the
        # operator knows persisted jobs were NOT actually audited.
        return WARN, (
            "no ghost-tool findings; persisted-job audit SKIPPED — "
            "scheduler jobstore could not be introspected (clean dev "
            "clone OR jobstore DB unreachable)"
        )

    return OK, (
        f"no ghost-tool findings; persisted-job audit clean "
        f"({len(persisted)} job(s) scanned)"
    )


def section_per_job_trigger_audit(*, strict: bool) -> tuple[str, str]:
    """Section 2. Dry-run ``_match_triggers`` on every persisted job.

    Informative by default; ``--strict`` upgrades hits to ``[FAIL]``.
    Reads the jobstore directly via ``_read_persisted_jobs`` so a
    stopped scheduler still sees the persisted rows.
    """
    try:
        from app.scheduler_instance import scheduler
        from app.tasks import _match_triggers
    except Exception as exc:
        return FAIL, f"import failed: {type(exc).__name__}: {exc}"

    persisted = _read_persisted_jobs(scheduler)
    if persisted is None:
        return WARN, (
            "persisted-job trigger audit SKIPPED — scheduler "
            "jobstore could not be introspected (clean dev clone "
            "OR jobstore DB unreachable)"
        )
    jobs = persisted

    if not jobs:
        return OK, "no persisted jobs in scheduler store"

    hits: list[tuple[str, list[tuple[str, str]]]] = []
    for job in jobs:
        job_id = getattr(job, "id", "<unknown>")
        kw = getattr(job, "kwargs", None) or {}
        prompt = kw.get("task_prompt") or ""
        if not isinstance(prompt, str) or not prompt:
            continue
        matches = _match_triggers(prompt)
        if not matches:
            continue
        # (matched_text, kind) summary per job.
        summary = [(m[0], m[2]) for m in matches]
        hits.append((job_id, summary))

    if not hits:
        return OK, f"scanned {len(jobs)} job(s); no trigger matches"

    lines = [
        f"scanned {len(jobs)} job(s); {len(hits)} would trip the fabrication detector:",
    ]
    for job_id, summary in hits:
        triggers = ", ".join(f"{t!r}({k})" for t, k in summary)
        lines.append(f"  {job_id}: {triggers}")

    status = FAIL if strict else WARN
    return status, "\n".join(lines)


def section_pytest() -> tuple[str, str]:
    """Section 3. Run the test suite."""
    try:
        proc = subprocess.run(
            ["uv", "run", "python", "-m", "pytest", "tests/", "-q"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return FAIL, "uv not available on PATH; cannot run pytest"
    except Exception as exc:
        return FAIL, f"pytest subprocess raised: {type(exc).__name__}: {exc}"

    # Pytest summary lives in the last few lines of stdout. Grab tail
    # for the human; full output only shown on failure.
    tail = "\n".join(proc.stdout.strip().splitlines()[-5:]) if proc.stdout else ""

    if proc.returncode == 0:
        return OK, tail or "pytest exit 0"
    body = "\n".join(filter(None, [
        f"pytest exit {proc.returncode}",
        "--- stdout tail ---",
        tail,
        "--- stderr ---",
        (proc.stderr or "").strip(),
    ]))
    return FAIL, body


def section_working_tree() -> tuple[str, str]:
    """Section 4. ``git status --porcelain`` snapshot."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        return FAIL, "git not available on PATH"
    except subprocess.CalledProcessError as exc:
        return FAIL, f"git status failed: {exc.stderr or exc}"

    lines = proc.stdout.splitlines()
    if not lines:
        return OK, "clean working tree"

    dirty_in_scope: list[str] = []
    untracked: list[str] = []
    other: list[str] = []
    for raw in lines:
        # Porcelain v1: ``XY path`` — first two chars are the staged +
        # unstaged status codes, third char is a space, rest is the
        # path. Renames look like ``XY old -> new``; we keep the raw
        # line for the dirty/other lists and only extract a path for
        # the scope check.
        if len(raw) < 4:
            continue
        status_xy = raw[:2]
        path = raw[3:].strip()
        if not path:
            continue
        # Rename: take the "new" half for scope classification.
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if status_xy == "??":
            untracked.append(path)
            continue
        if path.startswith("app/") or path.startswith("docs/"):
            dirty_in_scope.append(raw)
        else:
            other.append(raw)

    summary_lines: list[str] = []
    status = OK
    if dirty_in_scope:
        status = WARN
        summary_lines.append("dirty in app/ or docs/ (out-of-scope drift?):")
        for line in dirty_in_scope:
            summary_lines.append(f"  {line}")
    if other:
        summary_lines.append("other modifications:")
        for line in other:
            summary_lines.append(f"  {line}")
    if untracked:
        summary_lines.append(f"untracked ({len(untracked)}):")
        for path in untracked:
            summary_lines.append(f"  {path}")
    if not summary_lines:
        return OK, "clean working tree"
    return status, "\n".join(summary_lines)


def section_recent_commits(*, ref: str = "origin/evo/amazon_manager") -> tuple[str, str]:
    """Section 5. ``git log --oneline <ref>..HEAD``."""
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", f"{ref}..HEAD"],
            capture_output=True, text=True, check=True,
        )
    except FileNotFoundError:
        return FAIL, "git not available on PATH"
    except subprocess.CalledProcessError as exc:
        return WARN, (
            f"git log {ref}..HEAD failed (remote ref may be missing): "
            f"{(exc.stderr or '').strip() or exc}"
        )

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    count = len(lines)
    if count == 0:
        return WARN, f"no commits ahead of {ref}; nothing to push"

    listing = "\n".join(f"  {ln}" for ln in lines)
    summary = f"{count} commit(s) ahead of {ref}:\n{listing}"

    if count < _AHEAD_LO or count > _AHEAD_HI:
        return WARN, (
            f"unexpected commit count ({count}, expected {_AHEAD_LO}-{_AHEAD_HI}):\n"
            f"{listing}"
        )
    return OK, summary


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


SECTION_TITLES: list[tuple[str, str]] = [
    ("boot_validator", "1) Boot validator dry-run"),
    ("per_job_trigger_audit", "2) Per-job trigger audit"),
    ("pytest", "3) Full pytest"),
    ("working_tree", "4) Working-tree snapshot"),
    ("recent_commits", "5) Recent commits vs origin"),
]


def _print_section(title: str, status: str, body: str) -> None:
    print(f"\n=== {title} ===")
    print(f"{status} {body}")


async def run_all(*, strict: bool, ref: str) -> int:
    """Run every section; return exit code (0 = success)."""
    statuses: dict[str, str] = {}

    s1_status, s1_body = await section_boot_validator()
    statuses["boot_validator"] = s1_status
    _print_section(SECTION_TITLES[0][1], s1_status, s1_body)

    s2_status, s2_body = section_per_job_trigger_audit(strict=strict)
    statuses["per_job_trigger_audit"] = s2_status
    _print_section(SECTION_TITLES[1][1], s2_status, s2_body)

    s3_status, s3_body = section_pytest()
    statuses["pytest"] = s3_status
    _print_section(SECTION_TITLES[2][1], s3_status, s3_body)

    s4_status, s4_body = section_working_tree()
    statuses["working_tree"] = s4_status
    _print_section(SECTION_TITLES[3][1], s4_status, s4_body)

    s5_status, s5_body = section_recent_commits(ref=ref)
    statuses["recent_commits"] = s5_status
    _print_section(SECTION_TITLES[4][1], s5_status, s5_body)

    print("\n=== Summary ===")
    for key, title in SECTION_TITLES:
        print(f"  {statuses[key]} {title}")

    any_fail = any(s == FAIL for s in statuses.values())
    any_warn = any(s == WARN for s in statuses.values())
    print()
    if any_fail:
        print(f"{FAIL} preflight failed — fix the FAIL section(s) before pushing.")
        return 1
    if any_warn:
        print(f"{WARN} preflight passed with warnings — review WARN section(s) before pushing.")
    else:
        print(f"{OK} preflight clean — safe to push.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_preflight",
        description="Pre-push preflight for evo/amazon_manager.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Upgrade per-job trigger-audit hits from WARN to FAIL.",
    )
    parser.add_argument(
        "--ref",
        default="origin/evo/amazon_manager",
        help="Git ref to compare HEAD against for the recent-commits "
             "section (default: origin/evo/amazon_manager).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run_all(strict=args.strict, ref=args.ref))


if __name__ == "__main__":
    sys.exit(main())
