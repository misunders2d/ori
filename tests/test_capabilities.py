"""Unit tests for app.core.capabilities.

Covers the v3 proposal §7.1 matrix:
    - empty store + admin implicit-all
    - grant / revoke persistence
    - concurrent grant under asyncio.Lock
    - atomic write: no stray .tmp on success
    - crash mid-write: original untouched, lock released
    - corrupt JSON first-load: empty + ERROR, file unchanged
    - corrupt JSON subsequent reload: stale cache preserved, file unchanged
    - grant after corrupt-load: atomic replace of bad file
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.core import capabilities


@pytest.fixture
def isolated_caps(tmp_path, monkeypatch):
    """Redirect CAPABILITIES_PATH to a temp file and reset the cache."""
    p = tmp_path / "capabilities.json"
    monkeypatch.setattr(capabilities, "CAPABILITIES_PATH", str(p))
    capabilities._reset_for_tests()
    monkeypatch.delenv("ADMIN_USER_IDS", raising=False)
    return p


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "tg_admin")


# --------------------------------------------------------------------------- basic


@pytest.mark.asyncio
async def test_empty_store_returns_false(isolated_caps):
    assert await capabilities.has_capability("tg_111", "send_to_groups") is False
    assert await capabilities.has_capability("tg_111", "manage_aliases") is False
    assert await capabilities.has_capability("tg_111", "forward_files") is False


@pytest.mark.asyncio
async def test_admin_has_every_capability(isolated_caps, admin_env):
    for cap in capabilities.CANONICAL_CAPABILITIES:
        assert await capabilities.has_capability("tg_admin", cap) is True
    # list_for returns the full canonical set
    assert set(await capabilities.list_for("tg_admin")) == set(
        capabilities.CANONICAL_CAPABILITIES
    )


@pytest.mark.asyncio
async def test_admin_implicit_for_unprefixed_numeric(isolated_caps, monkeypatch):
    """Whitelist-compat: numeric ADMIN_USER_IDS works with tg_-prefixed lookup."""
    monkeypatch.setenv("ADMIN_USER_IDS", "999")
    assert await capabilities.has_capability("tg_999", "send_to_groups") is True
    assert await capabilities.has_capability("999", "send_to_groups") is True


# --------------------------------------------------------------------------- grant/revoke


@pytest.mark.asyncio
async def test_grant_persists_and_reloads(isolated_caps):
    await capabilities.grant("tg_111", "send_to_groups")
    # in-memory check
    assert await capabilities.has_capability("tg_111", "send_to_groups") is True
    # on-disk check
    raw = json.loads(isolated_caps.read_text())
    assert raw == {"tg_111": ["send_to_groups"]}
    # drop cache and reload from disk
    capabilities._reset_for_tests()
    assert await capabilities.has_capability("tg_111", "send_to_groups") is True


@pytest.mark.asyncio
async def test_revoke_removes(isolated_caps):
    await capabilities.grant("tg_111", "send_to_groups")
    await capabilities.grant("tg_111", "manage_aliases")
    await capabilities.revoke("tg_111", "send_to_groups")
    raw = json.loads(isolated_caps.read_text())
    assert raw == {"tg_111": ["manage_aliases"]}
    assert await capabilities.has_capability("tg_111", "send_to_groups") is False
    assert await capabilities.has_capability("tg_111", "manage_aliases") is True


@pytest.mark.asyncio
async def test_revoke_last_capability_drops_user_row(isolated_caps):
    await capabilities.grant("tg_111", "send_to_groups")
    await capabilities.revoke("tg_111", "send_to_groups")
    raw = json.loads(isolated_caps.read_text())
    assert raw == {}


@pytest.mark.asyncio
async def test_revoke_noop_when_missing(isolated_caps):
    # Should not raise and should not create a row.
    await capabilities.revoke("tg_999", "send_to_groups")
    if isolated_caps.exists():
        raw = json.loads(isolated_caps.read_text())
        assert raw == {}


@pytest.mark.asyncio
async def test_unknown_capability_rejected(isolated_caps):
    with pytest.raises(ValueError):
        await capabilities.has_capability("tg_111", "does_not_exist")
    with pytest.raises(ValueError):
        await capabilities.grant("tg_111", "does_not_exist")
    with pytest.raises(ValueError):
        await capabilities.revoke("tg_111", "does_not_exist")


# --------------------------------------------------------------------------- concurrency


@pytest.mark.asyncio
async def test_concurrent_grants_both_observed(isolated_caps):
    """Two concurrent grants under the asyncio.Lock — both land."""
    await asyncio.gather(
        capabilities.grant("tg_111", "send_to_groups"),
        capabilities.grant("tg_222", "manage_aliases"),
    )
    raw = json.loads(isolated_caps.read_text())
    assert raw == {"tg_111": ["send_to_groups"], "tg_222": ["manage_aliases"]}


# --------------------------------------------------------------------------- atomic write


@pytest.mark.asyncio
async def test_atomic_write_no_stray_tmp(isolated_caps):
    await capabilities.grant("tg_111", "forward_files")
    # No leftover .tmp after a successful write.
    parent = isolated_caps.parent
    stragglers = [p for p in parent.iterdir() if p.name.endswith(".tmp")]
    assert stragglers == []


@pytest.mark.asyncio
async def test_crash_mid_write_preserves_original(
    isolated_caps, monkeypatch
):
    """If os.replace raises, the original file AND the in-memory cache
    must be untouched, and the lock must be released so subsequent calls
    can proceed."""
    # Seed an initial committed value.
    await capabilities.grant("tg_111", "send_to_groups")
    original_bytes = isolated_caps.read_bytes()

    # Force os.replace to raise for the next write.
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(capabilities.os, "replace", boom)

    with pytest.raises(OSError, match="simulated crash"):
        await capabilities.grant("tg_222", "forward_files")

    # Original on-disk content unchanged.
    assert isolated_caps.read_bytes() == original_bytes
    # In-memory cache rolled back: the failed grant is NOT visible.
    assert await capabilities.has_capability("tg_222", "forward_files") is False
    assert await capabilities.has_capability("tg_111", "send_to_groups") is True

    # Lock released -> next call works after restoring os.replace.
    monkeypatch.setattr(capabilities.os, "replace", real_replace)
    await capabilities.grant("tg_333", "manage_aliases")
    raw = json.loads(isolated_caps.read_text())
    # The failed grant is NOT silently persisted alongside the new one.
    assert raw == {
        "tg_111": ["send_to_groups"],
        "tg_333": ["manage_aliases"],
    }
    assert "tg_222" not in raw


@pytest.mark.asyncio
async def test_failed_grant_not_visible_in_memory(isolated_caps, monkeypatch):
    """After a failed grant, has_capability() must return False — without
    needing a reset/reload."""

    def boom(src, dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(capabilities.os, "replace", boom)

    with pytest.raises(OSError):
        await capabilities.grant("tg_222", "forward_files")

    assert await capabilities.has_capability("tg_222", "forward_files") is False


@pytest.mark.asyncio
async def test_failed_revoke_keeps_capability_in_memory_and_on_disk(
    isolated_caps, monkeypatch
):
    """If a revoke fails on disk, the capability must remain in memory AND
    on disk — no partial drop."""
    await capabilities.grant("tg_111", "send_to_groups")
    await capabilities.grant("tg_111", "manage_aliases")
    original_bytes = isolated_caps.read_bytes()

    def boom(src, dst):
        raise OSError("simulated crash")

    monkeypatch.setattr(capabilities.os, "replace", boom)

    with pytest.raises(OSError):
        await capabilities.revoke("tg_111", "send_to_groups")

    # In-memory: capability still present.
    assert await capabilities.has_capability("tg_111", "send_to_groups") is True
    assert await capabilities.has_capability("tg_111", "manage_aliases") is True
    # On-disk: file unchanged.
    assert isolated_caps.read_bytes() == original_bytes


# --------------------------------------------------------------------------- corrupt loads


@pytest.mark.asyncio
async def test_corrupt_first_load_returns_empty_and_leaves_file(isolated_caps):
    """First load with corrupt file -> empty in-memory + file untouched."""
    isolated_caps.write_text("{ this is not valid json")
    original_bytes = isolated_caps.read_bytes()
    # Trigger load via has_capability.
    assert await capabilities.has_capability("tg_111", "send_to_groups") is False
    assert isolated_caps.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_corrupt_reload_preserves_stale_cache(isolated_caps):
    """Cache populated, then file corrupted, then reload -> stale cache
    survives and the bad file is preserved."""
    await capabilities.grant("tg_111", "send_to_groups")
    isolated_caps.write_text("garbage{{{")
    bad_bytes = isolated_caps.read_bytes()

    await capabilities.reload()

    assert await capabilities.has_capability("tg_111", "send_to_groups") is True
    assert isolated_caps.read_bytes() == bad_bytes


@pytest.mark.asyncio
async def test_grant_after_corrupt_first_load_replaces_file(isolated_caps):
    """First-load corrupt + grant -> atomic write replaces the bad file."""
    isolated_caps.write_text("{ corrupt")
    await capabilities.grant("tg_111", "send_to_groups")
    raw = json.loads(isolated_caps.read_text())
    assert raw == {"tg_111": ["send_to_groups"]}


@pytest.mark.asyncio
async def test_corrupt_top_level_not_dict_is_treated_as_corrupt(isolated_caps):
    """A JSON list at the root is invalid for this store."""
    isolated_caps.write_text(json.dumps(["tg_111", "send_to_groups"]))
    assert await capabilities.has_capability("tg_111", "send_to_groups") is False


@pytest.mark.asyncio
async def test_corrupt_row_shape_is_treated_as_corrupt(isolated_caps):
    """A row whose value is not a list of strings is invalid."""
    isolated_caps.write_text(json.dumps({"tg_111": "send_to_groups"}))
    assert await capabilities.has_capability("tg_111", "send_to_groups") is False


# --------------------------------------------------------------------------- list / all_users


@pytest.mark.asyncio
async def test_list_for_non_admin(isolated_caps):
    await capabilities.grant("tg_111", "send_to_groups")
    await capabilities.grant("tg_111", "manage_aliases")
    assert sorted(await capabilities.list_for("tg_111")) == [
        "manage_aliases",
        "send_to_groups",
    ]
    assert await capabilities.list_for("tg_999") == []


@pytest.mark.asyncio
async def test_all_users_does_not_include_admins(isolated_caps, admin_env):
    await capabilities.grant("tg_111", "send_to_groups")
    users = await capabilities.all_users()
    assert users == ["tg_111"]
    # Admin is implicit, never stored on disk.
    assert "tg_admin" not in users
