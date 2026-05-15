"""Tests for ``app.v2.registry_cache.loader``.

Phase 6 slice 2 per ``docs/PHASE_6_PLAN.md`` §5.4.

Pins:
- Load + save round-trip per kind.
- Missing file returns ``None`` (not exception).
- Owner-id mismatch returns ``None``; file NOT deleted;
  WARNING-level caplog record emitted (round-2 reviewer
  L397).
- Matching owner_id emits zero WARNING records (inverse pin).
- ``save_cache`` raises ``ValueError`` BEFORE any I/O when
  ``snapshot.kind != kind`` (round-1 reviewer L314).
- Atomic write: simulated crash mid-rename preserves the
  previous good file; tmp lingers for forensic inspection.
- Successful save leaves no ``.tmp.*`` artifacts.
- Corrupt JSON / extra unknown field / wrong discriminator
  raise :class:`RegistryCacheError`.
- ``is_stale`` boundary semantics + AST pin (no
  ``datetime.now`` call inside the function body).
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import os
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from app.v2.registry_cache.errors import RegistryCacheError
from app.v2.registry_cache.loader import (
    is_stale,
    load_cache,
    save_cache,
)
from app.v2.registry_cache.paths import cache_path
from app.v2.registry_cache.schemas import (
    GoogleDocsCache,
    GoogleDocsEntry,
    GoogleSheetsCache,
    GoogleSheetsEntry,
    SlackChannelEntry,
    SlackChannelsCache,
)


_UTC_NOW = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _slack(**overrides) -> SlackChannelsCache:
    base = dict(
        workspace_id="T_OLD",
        fetched_at=_UTC_NOW,
        source="slack.api.conversations.list",
        etag=None,
        channels=[
            SlackChannelEntry(id="C001", name="general"),
            SlackChannelEntry(id="C002", name="alerts"),
        ],
    )
    base.update(overrides)
    return SlackChannelsCache(**base)


def _sheets(**overrides) -> GoogleSheetsCache:
    base = dict(
        account_id="sa@x.iam",
        fetched_at=_UTC_NOW,
        source="drive.api.files.list?mimeType=spreadsheet",
        etag="W/\"abc\"",
        items=[
            GoogleSheetsEntry(id="1aBc", name="weekly metrics"),
        ],
    )
    base.update(overrides)
    return GoogleSheetsCache(**base)


def _docs(**overrides) -> GoogleDocsCache:
    base = dict(
        account_id="sa@x.iam",
        fetched_at=_UTC_NOW,
        source="drive.api.files.list?mimeType=document",
        etag=None,
        docs=[
            GoogleDocsEntry(id="1xYz", name="weekly notes"),
        ],
    )
    base.update(overrides)
    return GoogleDocsCache(**base)


# ===========================================================================
# Round-trip
# ===========================================================================


def test_slack_round_trip(tmp_path):
    snap = _slack()
    save_cache("slack_channels", snap, base=tmp_path)
    loaded = load_cache("slack_channels", base=tmp_path)

    assert loaded == snap


def test_sheets_round_trip(tmp_path):
    snap = _sheets()
    save_cache("google_sheets_items", snap, base=tmp_path)
    loaded = load_cache("google_sheets_items", base=tmp_path)

    assert loaded == snap


def test_docs_round_trip(tmp_path):
    snap = _docs()
    save_cache("google_docs_items", snap, base=tmp_path)
    loaded = load_cache("google_docs_items", base=tmp_path)

    assert loaded == snap


# ===========================================================================
# Missing file
# ===========================================================================


def test_missing_file_returns_none_slack(tmp_path):
    assert load_cache("slack_channels", base=tmp_path) is None


def test_missing_file_returns_none_sheets(tmp_path):
    assert load_cache("google_sheets_items", base=tmp_path) is None


def test_missing_file_returns_none_docs(tmp_path):
    assert load_cache("google_docs_items", base=tmp_path) is None


# ===========================================================================
# Owner-id mismatch (L295 rename + L397 caplog)
# ===========================================================================


def test_owner_mismatch_returns_none_and_logs_warning_slack(
    tmp_path, caplog
):
    save_cache("slack_channels", _slack(workspace_id="T_OLD"), base=tmp_path)

    caplog.set_level(
        logging.WARNING, logger="app.v2.registry_cache.loader"
    )
    result = load_cache(
        "slack_channels",
        expected_owner_id="T_NEW",
        base=tmp_path,
    )

    assert result is None
    # File preserved (per Q7 — no rename, no delete).
    target = cache_path("slack_channels", base=tmp_path)
    assert target.exists()
    # Exactly one WARNING record at the expected logger.
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "app.v2.registry_cache.loader"
    ]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "slack_channels" in msg
    assert "T_NEW" in msg
    assert "T_OLD" in msg


def test_owner_mismatch_logs_warning_sheets(tmp_path, caplog):
    save_cache(
        "google_sheets_items",
        _sheets(account_id="sa_old@x.iam"),
        base=tmp_path,
    )

    caplog.set_level(
        logging.WARNING, logger="app.v2.registry_cache.loader"
    )
    result = load_cache(
        "google_sheets_items",
        expected_owner_id="sa_new@x.iam",
        base=tmp_path,
    )

    assert result is None
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "app.v2.registry_cache.loader"
    ]
    assert len(warnings) == 1


def test_owner_mismatch_logs_warning_docs(tmp_path, caplog):
    save_cache(
        "google_docs_items",
        _docs(account_id="sa_old@x.iam"),
        base=tmp_path,
    )

    caplog.set_level(
        logging.WARNING, logger="app.v2.registry_cache.loader"
    )
    result = load_cache(
        "google_docs_items",
        expected_owner_id="sa_new@x.iam",
        base=tmp_path,
    )

    assert result is None
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "app.v2.registry_cache.loader"
    ]
    assert len(warnings) == 1


def test_matching_owner_emits_no_warning(tmp_path, caplog):
    """Inverse pin — matching owner_id load must NOT log."""
    save_cache("slack_channels", _slack(workspace_id="T_OK"), base=tmp_path)

    caplog.set_level(
        logging.WARNING, logger="app.v2.registry_cache.loader"
    )
    result = load_cache(
        "slack_channels",
        expected_owner_id="T_OK",
        base=tmp_path,
    )

    assert result is not None
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "app.v2.registry_cache.loader"
    ]
    assert warnings == []


def test_no_expected_owner_id_skips_check(tmp_path, caplog):
    """``expected_owner_id=None`` means "trust the file";
    no comparison runs, no warning."""
    save_cache("slack_channels", _slack(workspace_id="T_X"), base=tmp_path)

    caplog.set_level(
        logging.WARNING, logger="app.v2.registry_cache.loader"
    )
    result = load_cache("slack_channels", base=tmp_path)

    assert result is not None
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "app.v2.registry_cache.loader"
    ]
    assert warnings == []


# ===========================================================================
# Kind-mismatch save guard (L314)
# ===========================================================================


def test_save_kind_mismatch_raises_before_io(tmp_path):
    """save_cache rejects when snapshot.kind != requested kind.
    Pin: no file is written."""
    docs_snap = _docs()
    with pytest.raises(ValueError, match="save_cache kind mismatch"):
        save_cache("slack_channels", docs_snap, base=tmp_path)

    # No files anywhere in tmp_path — guard ran before any I/O.
    assert list(tmp_path.iterdir()) == []


def test_save_kind_mismatch_docs_at_sheets_path(tmp_path):
    docs_snap = _docs()
    with pytest.raises(ValueError):
        save_cache("google_sheets_items", docs_snap, base=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_save_kind_mismatch_sheets_at_docs_path(tmp_path):
    sheets_snap = _sheets()
    with pytest.raises(ValueError):
        save_cache("google_docs_items", sheets_snap, base=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_save_matching_kind_writes_file(tmp_path):
    save_cache("slack_channels", _slack(), base=tmp_path)
    target = cache_path("slack_channels", base=tmp_path)
    assert target.exists()


# ===========================================================================
# Atomic write
# ===========================================================================


def test_successful_save_leaves_no_tmp_artifact(tmp_path):
    save_cache("slack_channels", _slack(), base=tmp_path)

    contents = sorted(p.name for p in tmp_path.iterdir())
    assert contents == ["slack_channels.json"]


def test_crash_mid_rename_preserves_previous_file(tmp_path, monkeypatch):
    """Simulate ``os.rename`` failing after the tmp file is
    written. The previous good file MUST stay; the tmp file
    is left for forensic inspection."""
    # 1. Seed a good file via a normal save.
    save_cache(
        "slack_channels",
        _slack(workspace_id="T_OLD", channels=[]),
        base=tmp_path,
    )
    good_path = cache_path("slack_channels", base=tmp_path)
    good_bytes = good_path.read_bytes()

    # 2. Monkeypatch rename to raise mid-save.
    from app.v2.registry_cache import loader as loader_mod

    original_rename = os.rename

    def _boom(src, dst):
        # Make sure the tmp file actually exists before we
        # raise, so the test can assert on it afterwards.
        assert os.path.exists(src), (
            f"tmp file {src!r} should exist before rename"
        )
        raise OSError("simulated rename failure")

    monkeypatch.setattr(loader_mod.os, "rename", _boom)

    # 3. Attempt a second save with new payload; expect OSError.
    new_snap = _slack(
        workspace_id="T_NEW",
        channels=[SlackChannelEntry(id="C999", name="boom")],
    )
    with pytest.raises(OSError, match="simulated rename failure"):
        save_cache("slack_channels", new_snap, base=tmp_path)

    # 4. Restore rename so subsequent test cleanup works.
    monkeypatch.setattr(loader_mod.os, "rename", original_rename)

    # 5. Verify: good file untouched.
    assert good_path.read_bytes() == good_bytes
    # 6. Tmp file still there (forensic inspection path).
    tmp_files = [
        p
        for p in tmp_path.iterdir()
        if p.name.startswith("slack_channels.json.tmp.")
    ]
    assert len(tmp_files) == 1


# ===========================================================================
# Parse errors
# ===========================================================================


def test_corrupt_json_raises_registry_cache_error(tmp_path):
    path = cache_path("slack_channels", base=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(RegistryCacheError, match="failed to parse"):
        load_cache("slack_channels", base=tmp_path)


def test_unknown_field_raises_registry_cache_error(tmp_path):
    """`extra="forbid"` surfaces as ValidationError → wrapped
    in RegistryCacheError by the loader."""
    path = cache_path("slack_channels", base=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "slack_channels",
        "workspace_id": "T_X",
        "fetched_at": _UTC_NOW.isoformat(),
        "source": "x",
        "etag": None,
        "channels": [],
        "unexpected_field": "boom",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryCacheError, match="failed to parse"):
        load_cache("slack_channels", base=tmp_path)


def test_wrong_discriminator_in_payload_raises(tmp_path):
    """A file at the slack path whose kind field says
    google_sheets_items raises (the literal discriminator
    catches the mismatch)."""
    path = cache_path("slack_channels", base=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "google_sheets_items",
        "workspace_id": "T_X",
        "fetched_at": _UTC_NOW.isoformat(),
        "source": "x",
        "etag": None,
        "channels": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryCacheError, match="failed to parse"):
        load_cache("slack_channels", base=tmp_path)


def test_non_utc_fetched_at_in_payload_raises(tmp_path):
    """Schema validator wraps as RegistryCacheError via the
    pydantic ValidationError catch."""
    path = cache_path("slack_channels", base=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    naive_iso = datetime(2026, 5, 15, 12, 0).isoformat()
    payload = {
        "kind": "slack_channels",
        "workspace_id": "T_X",
        "fetched_at": naive_iso,
        "source": "x",
        "etag": None,
        "channels": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RegistryCacheError, match="failed to parse"):
        load_cache("slack_channels", base=tmp_path)


# ===========================================================================
# is_stale (Q5)
# ===========================================================================


def test_is_stale_returns_false_when_now_equals_fetched_at():
    cache = _slack(fetched_at=_UTC_NOW)
    assert is_stale(cache, now=_UTC_NOW) is False


def test_is_stale_returns_false_at_ttl_boundary():
    """``now - fetched_at == ttl`` is NOT stale (strict >)."""
    cache = _slack(fetched_at=_UTC_NOW)
    assert (
        is_stale(cache, now=_UTC_NOW + timedelta(hours=24), ttl=timedelta(hours=24))
        is False
    )


def test_is_stale_returns_true_beyond_ttl():
    cache = _slack(fetched_at=_UTC_NOW)
    assert (
        is_stale(
            cache,
            now=_UTC_NOW + timedelta(hours=24, seconds=1),
            ttl=timedelta(hours=24),
        )
        is True
    )


def test_is_stale_default_ttl_is_24h():
    cache = _slack(fetched_at=_UTC_NOW)
    # Just past 24h with default ttl.
    assert (
        is_stale(cache, now=_UTC_NOW + timedelta(hours=24, seconds=1))
        is True
    )
    # Just under 24h.
    assert (
        is_stale(cache, now=_UTC_NOW + timedelta(hours=23, minutes=59))
        is False
    )


def test_is_stale_works_for_sheets_and_docs():
    """Pin: the helper is generic over CacheFile; sheets +
    docs caches use it identically."""
    s = _sheets(fetched_at=_UTC_NOW)
    d = _docs(fetched_at=_UTC_NOW)
    future = _UTC_NOW + timedelta(days=2)
    assert is_stale(s, now=future) is True
    assert is_stale(d, now=future) is True


def _function_body_calls(fn) -> set[str]:
    """AST helper — collect every ``Call`` whose callable is
    a dotted attribute (``a.b.c``) or bare name in the
    function body. Ignores docstrings and comments."""
    source = inspect.getsource(fn)
    tree = ast.parse(source)
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            parts: list[str] = []
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                parts.append(target.id)
                calls.add(".".join(reversed(parts)))
    return calls


def test_is_stale_does_not_call_datetime_now():
    """AST pin — phase-5 hard rule 10. ``_defaults.py`` is the
    sole site for ``datetime.now()``; `is_stale` is a pure
    arithmetic comparison over the caller's ``now``."""
    calls = _function_body_calls(is_stale)
    forbidden = {"datetime.now", "now", "datetime.utcnow"}
    leaked = calls & forbidden
    assert not leaked, f"is_stale must be clock-free; got {leaked!r}"


def test_loader_module_does_not_import_clock_module_level():
    """Belt-and-braces: the loader module imports `datetime` /
    `timedelta` for type-hinting + the default ttl, but does
    NOT import or use `app.v2.runtime._defaults`."""
    import sys

    # Reload-safe: just check the loader's globals.
    from app.v2.registry_cache import loader as loader_mod

    assert "app.v2.runtime._defaults" not in sys.modules or (
        # If imported elsewhere we still want to ensure THIS
        # module doesn't reach into it.
        "_defaults" not in dir(loader_mod)
    )
    # `_defaults` symbol is NOT bound in the loader module.
    assert not hasattr(loader_mod, "prod_clock")
