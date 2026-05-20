"""Unit tests for app.core.telegram_store aliases (proposal §7.2).

Covers:
    - save → list → resolve → delete → list-empty
    - owner-scoping
    - case-folding
    - last_used_at update on resolve
    - DM-type rejected on save
    - lazy schema init: open on fresh tmpdir produces tables
"""

from __future__ import annotations

import os

import aiosqlite
import pytest

from app.core import telegram_store


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "telegram_skills.db"
    monkeypatch.setattr(telegram_store, "DB_PATH", str(db_path))
    telegram_store._reset_for_tests()
    return db_path


@pytest.mark.asyncio
async def test_save_list_resolve_delete(isolated_db):
    await telegram_store.save_alias(
        owner_user_id="tg_111",
        alias="engineering",
        chat_id=-1001234,
        chat_type="supergroup",
        title="Eng Team",
        username="eng_chat",
    )

    rows = await telegram_store.list_aliases("tg_111")
    assert len(rows) == 1
    assert rows[0]["alias"] == "engineering"
    assert rows[0]["chat_id"] == -1001234
    assert rows[0]["chat_type"] == "supergroup"
    assert rows[0]["title"] == "Eng Team"
    assert rows[0]["username"] == "eng_chat"
    assert rows[0]["last_used_at"] is None

    resolved = await telegram_store.resolve_alias("tg_111", "engineering")
    assert resolved is not None
    assert resolved["chat_id"] == -1001234
    assert resolved["last_used_at"] is not None  # set on resolve

    deleted = await telegram_store.delete_alias("tg_111", "engineering")
    assert deleted is True
    assert await telegram_store.list_aliases("tg_111") == []


@pytest.mark.asyncio
async def test_owner_scoping_isolates_namespaces(isolated_db):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    await telegram_store.save_alias(
        "tg_222", "engineering", -1009999, "channel"
    )

    a = await telegram_store.resolve_alias("tg_111", "engineering")
    b = await telegram_store.resolve_alias("tg_222", "engineering")
    assert a["chat_id"] == -1001234
    assert a["chat_type"] == "supergroup"
    assert b["chat_id"] == -1009999
    assert b["chat_type"] == "channel"

    # Deleting one does not affect the other.
    assert await telegram_store.delete_alias("tg_111", "engineering") is True
    assert (
        await telegram_store.resolve_alias("tg_222", "engineering")
        is not None
    )


@pytest.mark.asyncio
async def test_case_folding_collapses_to_same_row(isolated_db):
    await telegram_store.save_alias(
        "tg_111", "Engineering", -1001234, "supergroup"
    )
    await telegram_store.save_alias(
        "tg_111", "engineering", -1005678, "channel"
    )
    rows = await telegram_store.list_aliases("tg_111")
    assert len(rows) == 1
    # Second save overwrote.
    assert rows[0]["chat_id"] == -1005678
    assert rows[0]["chat_type"] == "channel"

    # Resolution is also case-folded.
    by_upper = await telegram_store.resolve_alias("tg_111", "ENGINEERING")
    assert by_upper is not None
    assert by_upper["chat_id"] == -1005678


@pytest.mark.asyncio
async def test_resolve_miss_returns_none(isolated_db):
    assert await telegram_store.resolve_alias("tg_111", "nonexistent") is None


@pytest.mark.asyncio
async def test_delete_miss_returns_false(isolated_db):
    assert await telegram_store.delete_alias("tg_111", "nonexistent") is False


@pytest.mark.asyncio
async def test_dm_chat_type_rejected_on_save(isolated_db):
    with pytest.raises(ValueError, match="chat_type"):
        await telegram_store.save_alias(
            "tg_111", "myself", 111, "user"
        )
    with pytest.raises(ValueError, match="chat_type"):
        await telegram_store.save_alias(
            "tg_111", "myself", 111, "private"
        )


@pytest.mark.asyncio
async def test_empty_alias_rejected(isolated_db):
    with pytest.raises(ValueError, match="alias"):
        await telegram_store.save_alias(
            "tg_111", "", -1001234, "supergroup"
        )
    with pytest.raises(ValueError, match="alias"):
        await telegram_store.save_alias(
            "tg_111", "   ", -1001234, "supergroup"
        )


@pytest.mark.asyncio
async def test_lazy_schema_init_on_fresh_tmpdir(isolated_db):
    """First call creates the DB and tables; no import-time write."""
    assert not isolated_db.exists()
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup"
    )
    assert isolated_db.exists()

    # Tables present.
    async with aiosqlite.connect(str(isolated_db)) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cursor:
            tables = [row[0] for row in await cursor.fetchall()]
    assert "chat_aliases" in tables
    assert "outbound_files" in tables


@pytest.mark.asyncio
async def test_chat_id_must_be_int(isolated_db):
    with pytest.raises(ValueError, match="chat_id"):
        await telegram_store.save_alias(
            "tg_111", "engineering", "-1001234", "supergroup"
        )


@pytest.mark.asyncio
async def test_save_alias_idempotent_upsert(isolated_db):
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "supergroup", title="Old"
    )
    await telegram_store.save_alias(
        "tg_111", "engineering", -1001234, "channel", title="New"
    )
    rows = await telegram_store.list_aliases("tg_111")
    assert len(rows) == 1
    assert rows[0]["chat_type"] == "channel"
    assert rows[0]["title"] == "New"
