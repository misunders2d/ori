"""Regression tests for the evolution sandbox bootstrap.

Pre-2026-05-14 ``_bootstrap_symlink_recursive`` was one-level deep:
walk ``PROJECT_ROOT``, symlink each top-level child into the
sandbox if missing. The moment staging materialised a real
directory inside the sandbox (e.g. ``sandbox/app/callbacks/`` to
host a staged ``app/callbacks/guardrails/admin.py``), the bootstrap
saw ``sandbox/app/callbacks`` exists and skipped — never recursing
into it to backfill the missing sibling files. ``app.callbacks``
was an incomplete package in the sandbox, pytest collection raised
``KeyError: 'app'``, bezos labelled it "pre-existing", and we lost
3 hours.

These tests pin the recursion. Each fixture seeds a synthetic
live + sandbox pair, calls the helper, and asserts the missing
sibling files are visible afterwards.
"""

from __future__ import annotations

import os
from pathlib import Path


def _touch(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_bootstrap_symlinks_missing_top_level(tmp_path):
    """Empty sandbox → live tree is mirrored as top-level symlinks."""
    from app.tools.evolution import _bootstrap_symlink_recursive

    live = tmp_path / "live"
    sandbox = tmp_path / "sandbox"
    _touch(live / "a.py", "a")
    _touch(live / "pkg" / "__init__.py")
    _touch(live / "pkg" / "child.py")
    sandbox.mkdir()

    _bootstrap_symlink_recursive(str(live), str(sandbox))

    assert (sandbox / "a.py").exists()
    assert (sandbox / "pkg" / "child.py").exists()


def test_bootstrap_recurses_into_existing_real_dir(tmp_path):
    """Direct repro of the 2026-05-13 ``KeyError: 'app'`` bug.

    Pre-stage ``sandbox/app/callbacks/guardrails/admin.py`` as a
    real file (mirroring what evolution_stage_change does), then
    bootstrap. The sibling files of admin.py — ``__init__.py``,
    ``core.py`` — must appear in the sandbox via symlinks.
    """
    from app.tools.evolution import _bootstrap_symlink_recursive

    live = tmp_path / "live"
    _touch(live / "app" / "__init__.py")
    _touch(live / "app" / "callbacks" / "__init__.py")
    _touch(live / "app" / "callbacks" / "guardrails" / "__init__.py")
    _touch(live / "app" / "callbacks" / "guardrails" / "admin.py", "live version")
    _touch(live / "app" / "callbacks" / "guardrails" / "core.py", "core")

    sandbox = tmp_path / "sandbox"
    # Simulate staging: real file + real intermediate dirs.
    _touch(
        sandbox / "app" / "callbacks" / "guardrails" / "admin.py",
        "staged version",
    )

    _bootstrap_symlink_recursive(str(live), str(sandbox))

    # The staged file is untouched.
    assert (sandbox / "app" / "callbacks" / "guardrails" / "admin.py").read_text() == (
        "staged version"
    )
    # Sibling files appeared via symlinks.
    assert (sandbox / "app" / "callbacks" / "guardrails" / "core.py").exists()
    assert (sandbox / "app" / "callbacks" / "guardrails" / "__init__.py").exists()
    # Parent ``__init__.py`` files exist (so ``app.callbacks`` is a
    # complete package).
    assert (sandbox / "app" / "__init__.py").exists()
    assert (sandbox / "app" / "callbacks" / "__init__.py").exists()


def test_bootstrap_preserves_staged_file_over_live(tmp_path):
    """The recursion must not overwrite a real staged file with a
    symlink to the live version — staging is the source of truth
    for that file."""
    from app.tools.evolution import _bootstrap_symlink_recursive

    live = tmp_path / "live"
    _touch(live / "app" / "x.py", "LIVE")
    sandbox = tmp_path / "sandbox"
    _touch(sandbox / "app" / "x.py", "STAGED")

    _bootstrap_symlink_recursive(str(live), str(sandbox))

    assert (sandbox / "app" / "x.py").read_text() == "STAGED"


def test_bootstrap_respects_skip_top_level(tmp_path):
    """Top-level entries in ``skip_top_level`` (and dotfiles when
    ``.`` is present) are not mirrored."""
    from app.tools.evolution import _bootstrap_symlink_recursive

    live = tmp_path / "live"
    _touch(live / ".gitignore", "*.pyc")
    _touch(live / "data" / "ignored.txt", "x")
    _touch(live / "tests" / "test_x.py", "x")
    _touch(live / "app" / "x.py", "x")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    _bootstrap_symlink_recursive(
        str(live),
        str(sandbox),
        skip_top_level={".", "data", "tests"},
    )

    assert (sandbox / "app" / "x.py").exists()
    assert not (sandbox / "data").exists()
    assert not (sandbox / "tests").exists()
    assert not (sandbox / ".gitignore").exists()


def test_bootstrap_does_not_recurse_into_existing_symlinked_dir(tmp_path):
    """When the sandbox already holds a symlink to the live dir
    (typical when the sandbox isn't materialised deeply for that
    subtree), we leave it alone. Recursing in via the symlink would
    pointlessly create same-target inner symlinks."""
    from app.tools.evolution import _bootstrap_symlink_recursive

    live = tmp_path / "live"
    _touch(live / "pkg" / "x.py", "x")
    _touch(live / "pkg" / "y.py", "y")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    os.symlink(str(live / "pkg"), str(sandbox / "pkg"), target_is_directory=True)

    _bootstrap_symlink_recursive(str(live), str(sandbox))

    # The sandbox's pkg/ is still the symlink — not replaced, not
    # populated with inner re-symlinks.
    assert os.path.islink(sandbox / "pkg")


def test_bootstrap_reports_links_created(tmp_path):
    """``links_created`` accumulates every dst path the helper
    actually created (useful for logging / debugging)."""
    from app.tools.evolution import _bootstrap_symlink_recursive

    live = tmp_path / "live"
    _touch(live / "a.py")
    _touch(live / "pkg" / "b.py")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    created: list[str] = []
    _bootstrap_symlink_recursive(str(live), str(sandbox), links_created=created)

    # Top-level a.py and pkg/ are both symlinked (one whole pkg
    # symlink since the sandbox didn't have pkg/ as a real dir).
    assert any(p.endswith("a.py") for p in created)
    assert any(p.endswith(os.sep + "pkg") for p in created)
