"""Force-align the current checkout with its upstream — worktree-aware.

History:
- The original implementation issued `git reset --hard origin/master`
  unconditionally. The bot now runs from a git worktree on a feature
  branch (e.g. `evo/amazon_manager`), so resetting to master rewrote
  the wrong ref and wiped the working tree on 2026-05-11.
- The check is rebuilt here to (a) detect the current branch + its
  upstream and (b) align only that branch with only its tracking
  remote — never master unless the worktree is actually on master.

Two safety layers stay in place even after the worktree-awareness fix:
1. `@pytest.mark.infra` — declared in `pyproject.toml` (registered
   marker, `--strict-markers` enforced) so a `pytest tests/` run
   without `-m infra` doesn't pick it up.
2. `ORI_ALLOW_DESTRUCTIVE_SYNC=1` env gate — without it, the test
   skips before doing anything that mutates refs.

Use case: a maintainer on the same machine wants a one-shot "snap this
worktree back to the remote tip", typically after a broken local
commit they don't want to keep. Run via:

    ORI_ALLOW_DESTRUCTIVE_SYNC=1 uv run python -m pytest tests/test_force_sync.py -m infra -s
"""

import os
import subprocess

import pytest


def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a git subcommand in `cwd` and capture both streams (no shell=True)."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _project_root() -> str:
    """Resolve THIS worktree's root via `git rev-parse --show-toplevel`.

    Avoids relying on `os.getcwd()` (pytest may chdir into tmp paths)
    and the older hardcoded `app/../..` traversal (worktree-unfriendly).
    """
    res = _run(["git", "rev-parse", "--show-toplevel"])
    return res.stdout.strip() or os.getcwd()


def _current_branch(cwd: str) -> str | None:
    res = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    name = res.stdout.strip()
    return name or None


def _upstream_for(branch: str, cwd: str) -> str | None:
    """Return e.g. `origin/evo/amazon_manager` if `branch` has an upstream."""
    res = _run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}"],
        cwd=cwd,
    )
    if res.returncode != 0:
        return None
    upstream = res.stdout.strip()
    return upstream or None


@pytest.mark.infra
@pytest.mark.skipif(
    os.environ.get("ORI_ALLOW_DESTRUCTIVE_SYNC", "") != "1",
    reason=(
        "Destructive: force-aligns the current branch with its tracking remote. "
        "Set ORI_ALLOW_DESTRUCTIVE_SYNC=1 to opt in. Prefer the supervisor's "
        "exit-100 flow for real updates (deploy/ori-supervisor.py:apply_evolution)."
    ),
)
def test_force_sync():
    """Force-align THIS worktree's branch with its upstream, not `origin/master`.

    Worktree-aware: discovers the current branch + upstream via plumbing
    commands and refuses to proceed when either is missing. A detached
    HEAD, a branch without upstream, or `HEAD` itself are explicit skip
    reasons — they were silent disasters under the old script.
    """
    root = _project_root()
    branch = _current_branch(root)
    if not branch or branch == "HEAD":
        pytest.skip(f"detached HEAD or no current branch in {root}; nothing to align.")

    upstream = _upstream_for(branch, root)
    if not upstream:
        pytest.skip(
            f"branch '{branch}' has no upstream configured. "
            "Set one with `git branch --set-upstream-to origin/<branch>` before re-running."
        )

    remote, _, remote_branch = upstream.partition("/")
    if not remote or not remote_branch:
        pytest.skip(f"unparseable upstream ref '{upstream}' — bailing out.")

    # 1. Fetch only the matching remote branch — never a blanket fetch
    #    that might rewrite unrelated refs.
    fetch = _run(["git", "fetch", remote, remote_branch], cwd=root)
    assert fetch.returncode == 0, f"git fetch failed: {fetch.stderr.strip()}"

    # 2. Reset THIS branch to its upstream. `git reset --hard <ref>`
    #    only rewrites the currently checked-out branch; safe in a worktree.
    reset = _run(["git", "reset", "--hard", upstream], cwd=root)
    assert reset.returncode == 0, f"git reset failed: {reset.stderr.strip()}"

    # 3. Clean untracked files but preserve `data/` (vault, scheduler db, etc.)
    #    and `.env*` files — the worktree-agnostic excludes from before.
    _run(
        ["git", "clean", "-fd", "--exclude=data", "--exclude=.env", "--exclude=.env.*"],
        cwd=root,
    )

    # 4. Sanity check — branch is now at the upstream commit.
    head = _run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    up_sha = _run(["git", "rev-parse", upstream], cwd=root).stdout.strip()
    assert head == up_sha, f"after reset, HEAD={head} != upstream={up_sha}"
