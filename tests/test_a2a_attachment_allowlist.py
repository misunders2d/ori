"""A2A attachment path allowlist — regression test for the local-file
exfiltration gap surfaced during the 2026-05-11 hardening audit.

``app/tools/a2a.py:_load_attachment_bytes`` used to accept any local
path the LLM named, then read its bytes verbatim and ship them to a
friend agent. With prompt injection that path becomes ``/etc/passwd``,
``data/vault/credentials.json``, the user's SSH keys, etc. The fix is
``_validate_attachment_path`` — a small allowlist of dirs the rest of
the agent legitimately writes A2A-shareable artefacts into.

These tests don't exercise the network send; they only cover the
gate-keeping logic that decides whether a given path is loadable.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from app.tools.a2a import _load_attachment_bytes, _validate_attachment_path


# ---------------------------------------------------------------------------
# Setup: build a sandboxed cwd whose tmp/uploads + tmp/scratchpads +
# tmp/plans + data/exports match the allowlist roots the module read at
# import time. Each test puts a file inside one of those, plus an evil
# file outside.
# ---------------------------------------------------------------------------


@pytest.fixture
def allowlist_setup(tmp_path, monkeypatch):
    """Build the allowlist root structure under ``tmp_path`` and chdir
    there so relative paths resolve correctly.

    Returns a dict of named paths the individual tests use.
    """
    monkeypatch.chdir(tmp_path)
    roots = {
        "uploads": tmp_path / "tmp" / "uploads",
        "scratchpads": tmp_path / "tmp" / "scratchpads",
        "plans": tmp_path / "tmp" / "plans",
        "exports": tmp_path / "data" / "exports",
    }
    for r in roots.values():
        r.mkdir(parents=True)

    # The module's _A2A_ATTACHMENT_ALLOWLIST was frozen at import time
    # to whatever cwd was then. Repoint it to *this* test's roots so
    # the assertions actually exercise the guard logic against our
    # tmp dirs rather than the original repo's tmp/.
    import app.tools.a2a as a2a_mod

    monkeypatch.setattr(
        a2a_mod,
        "_A2A_ATTACHMENT_ALLOWLIST",
        tuple(os.path.realpath(str(p)) for p in roots.values()),
    )

    return roots


# ---------------------------------------------------------------------------
# Allowed paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "root_key,filename",
    [
        ("uploads", "img.png"),
        ("scratchpads", "session/abc.md"),
        ("plans", "plan.json"),
        ("exports", "report.csv"),
    ],
)
def test_validate_accepts_files_under_allowlisted_roots(
    allowlist_setup, root_key, filename
):
    roots = allowlist_setup
    target = roots[root_key] / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"payload")

    resolved = _validate_attachment_path(str(target))
    assert resolved == os.path.realpath(str(target))


def test_load_attachment_bytes_reads_allowlisted_file(allowlist_setup):
    target = allowlist_setup["uploads"] / "small.bin"
    target.write_bytes(b"hello")
    data, name, mime = _load_attachment_bytes({"path": str(target)})
    assert data == b"hello"
    assert name == "small.bin"
    assert mime  # mimetypes returns something or octet-stream fallback


# ---------------------------------------------------------------------------
# Refused paths
# ---------------------------------------------------------------------------


def test_validate_rejects_etc_passwd(allowlist_setup):
    with pytest.raises(ValueError, match="outside the A2A allowlist"):
        _validate_attachment_path("/etc/passwd")


def test_validate_rejects_path_traversal(allowlist_setup, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("creds")
    # `tmp/uploads/../../secret` resolves to tmp_path/secret which is
    # outside any allowlist root.
    traversal = allowlist_setup["uploads"] / ".." / ".." / "secret"
    with pytest.raises(ValueError, match="outside the A2A allowlist"):
        _validate_attachment_path(str(traversal))


def test_validate_rejects_symlink_escape(allowlist_setup, tmp_path):
    """Putting a symlink inside the allowlist that points *outside* must
    not let the caller read the outside file. The guard resolves symlinks
    before checking, so commonpath sees the real escape destination.
    """
    secret = tmp_path / "outside"
    secret.write_text("creds")
    link = allowlist_setup["uploads"] / "link_to_outside"
    os.symlink(str(secret), str(link))

    with pytest.raises(ValueError, match="outside the A2A allowlist"):
        _validate_attachment_path(str(link))


def test_validate_rejects_sibling_prefix_collision(allowlist_setup, tmp_path):
    """A directory like `tmp/uploads-evil` must not be allowlisted just
    because its name starts with `tmp/uploads`. We use commonpath, not
    startswith, precisely to defuse this.
    """
    sibling = tmp_path / "tmp" / "uploads-evil"
    sibling.mkdir()
    bad = sibling / "stolen.txt"
    bad.write_text("nope")
    with pytest.raises(ValueError, match="outside the A2A allowlist"):
        _validate_attachment_path(str(bad))


def test_validate_rejects_empty_path(allowlist_setup):
    with pytest.raises(ValueError, match="non-empty"):
        _validate_attachment_path("")


def test_load_attachment_bytes_rejects_etc_passwd(allowlist_setup):
    with pytest.raises(ValueError, match="outside the A2A allowlist"):
        _load_attachment_bytes({"path": "/etc/passwd"})


# ---------------------------------------------------------------------------
# Data-shape attachments still work (no path involved)
# ---------------------------------------------------------------------------


def test_load_attachment_bytes_inline_bytes_unaffected(allowlist_setup):
    data, name, mime = _load_attachment_bytes(
        {"data": b"binary", "name": "x.bin", "mime_type": "application/octet-stream"}
    )
    assert data == b"binary"
    assert name == "x.bin"
    assert mime == "application/octet-stream"


def test_load_attachment_bytes_base64_string_unaffected(allowlist_setup):
    import base64

    b64 = base64.b64encode(b"hi").decode()
    data, _, _ = _load_attachment_bytes({"data": b64, "name": "x.bin"})
    assert data == b"hi"
