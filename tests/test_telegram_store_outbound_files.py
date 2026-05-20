"""Unit tests for app.core.telegram_store outbound_files (proposal §7.3).

Covers:
    - put → get → expires_at = created_at + TTL
    - expired entry pruned on next get
    - TTL configurable via env
    - TTL sliding extension on hit
    - different owners isolate
    - sticker type rejected on insert (not in enum)
    - per-file_type round-trip (photo / document / audio / video / voice /
      video_note) — proposal §7.3 expanded entry
    - last_forward_cache stash / peek / pop with 5-min TTL
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core import telegram_store


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "telegram_skills.db"
    monkeypatch.setattr(telegram_store, "DB_PATH", str(db_path))
    monkeypatch.delenv("TELEGRAM_FILE_CACHE_TTL_HOURS", raising=False)
    telegram_store._reset_for_tests()
    return db_path


# --------------------------------------------------------------------------- basic put/get


@pytest.mark.asyncio
async def test_put_get_expires_default_ttl(isolated_db):
    row = await telegram_store.put_file(
        owner_user_id="tg_111",
        file_ref="q1_revenue",
        file_id="AgACAg...PHOTO_ID",
        file_type="photo",
        filename="q1_revenue.png",
        mime_type="image/png",
    )
    created = datetime.fromisoformat(row["created_at"])
    expires = datetime.fromisoformat(row["expires_at"])
    assert expires - created == timedelta(
        hours=telegram_store.DEFAULT_FILE_TTL_HOURS
    )

    fetched = await telegram_store.get_file(
        "tg_111", "q1_revenue", slide=False
    )
    assert fetched is not None
    assert fetched["file_id"] == "AgACAg...PHOTO_ID"
    assert fetched["file_type"] == "photo"
    assert fetched["filename"] == "q1_revenue.png"


@pytest.mark.asyncio
async def test_get_miss_returns_none(isolated_db):
    assert await telegram_store.get_file("tg_111", "missing") is None


@pytest.mark.asyncio
async def test_expired_entry_pruned_on_get(isolated_db, monkeypatch):
    """Force a row to expire in the past, then verify get prunes + misses."""
    await telegram_store.put_file(
        "tg_111", "stale", "FILE_ID", "document"
    )
    # Stamp expires_at into the past directly via SQL.
    import aiosqlite as _aiosqlite

    past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(
        timespec="microseconds"
    )
    async with _aiosqlite.connect(str(isolated_db)) as conn:
        await conn.execute(
            "UPDATE outbound_files SET expires_at = ? WHERE file_ref = ?",
            (past, "stale"),
        )
        await conn.commit()

    fetched = await telegram_store.get_file("tg_111", "stale")
    assert fetched is None

    # Row was deleted, not just hidden.
    async with _aiosqlite.connect(str(isolated_db)) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM outbound_files WHERE file_ref = ?",
            ("stale",),
        ) as cursor:
            count = (await cursor.fetchone())[0]
    assert count == 0


@pytest.mark.asyncio
async def test_ttl_env_override(isolated_db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_FILE_CACHE_TTL_HOURS", "1")
    row = await telegram_store.put_file(
        "tg_111", "short", "FILE_ID", "audio"
    )
    created = datetime.fromisoformat(row["created_at"])
    expires = datetime.fromisoformat(row["expires_at"])
    assert expires - created == timedelta(hours=1)


@pytest.mark.asyncio
async def test_ttl_env_invalid_falls_back_to_default(isolated_db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_FILE_CACHE_TTL_HOURS", "not-a-number")
    row = await telegram_store.put_file(
        "tg_111", "any", "FILE_ID", "voice"
    )
    created = datetime.fromisoformat(row["created_at"])
    expires = datetime.fromisoformat(row["expires_at"])
    assert expires - created == timedelta(
        hours=telegram_store.DEFAULT_FILE_TTL_HOURS
    )


@pytest.mark.asyncio
async def test_ttl_sliding_extension_on_hit(isolated_db, monkeypatch):
    await telegram_store.put_file(
        "tg_111", "chart", "FILE_ID", "photo"
    )
    first = await telegram_store.get_file("tg_111", "chart", slide=False)
    assert first is not None

    # Manually backdate the row, then a sliding get should refresh.
    import aiosqlite as _aiosqlite

    backdated = (
        datetime.now(timezone.utc) - timedelta(days=3)
    ).isoformat(timespec="microseconds")
    new_expires = (
        datetime.now(timezone.utc) + timedelta(hours=4)
    ).isoformat(timespec="microseconds")
    async with _aiosqlite.connect(str(isolated_db)) as conn:
        await conn.execute(
            "UPDATE outbound_files SET created_at = ?, expires_at = ? WHERE file_ref = ?",
            (backdated, new_expires, "chart"),
        )
        await conn.commit()

    bumped = await telegram_store.get_file("tg_111", "chart", slide=True)
    assert bumped is not None
    bumped_expires = datetime.fromisoformat(bumped["expires_at"])
    # Slide extends to roughly now + TTL, so well past the 4h horizon.
    assert bumped_expires > datetime.now(timezone.utc) + timedelta(days=6)


# --------------------------------------------------------------------------- owner isolation


@pytest.mark.asyncio
async def test_different_owners_isolate(isolated_db):
    await telegram_store.put_file(
        "tg_111", "chart", "OWNER1_FILE", "photo"
    )
    await telegram_store.put_file(
        "tg_222", "chart", "OWNER2_FILE", "document"
    )
    a = await telegram_store.get_file("tg_111", "chart")
    b = await telegram_store.get_file("tg_222", "chart")
    assert a["file_id"] == "OWNER1_FILE"
    assert a["file_type"] == "photo"
    assert b["file_id"] == "OWNER2_FILE"
    assert b["file_type"] == "document"


# --------------------------------------------------------------------------- file_type enum


@pytest.mark.asyncio
async def test_sticker_type_rejected(isolated_db):
    with pytest.raises(ValueError, match="file_type"):
        await telegram_store.put_file(
            "tg_111", "wave", "STICKER_ID", "sticker"
        )


@pytest.mark.asyncio
async def test_unknown_type_rejected(isolated_db):
    with pytest.raises(ValueError, match="file_type"):
        await telegram_store.put_file(
            "tg_111", "weird", "ID", "animation"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_type",
    sorted(telegram_store.ALLOWED_FILE_TYPES),
)
async def test_per_file_type_roundtrip(isolated_db, file_type):
    """Each allowed file_type round-trips exactly through put → get."""
    await telegram_store.put_file(
        "tg_111", f"ref_{file_type}", f"FILE_ID_{file_type}", file_type
    )
    fetched = await telegram_store.get_file(
        "tg_111", f"ref_{file_type}", slide=False
    )
    assert fetched is not None
    assert fetched["file_type"] == file_type
    assert fetched["file_id"] == f"FILE_ID_{file_type}"


# --------------------------------------------------------------------------- delete / list / prune


@pytest.mark.asyncio
async def test_delete_file(isolated_db):
    await telegram_store.put_file(
        "tg_111", "chart", "FILE_ID", "photo"
    )
    assert await telegram_store.delete_file("tg_111", "chart") is True
    assert await telegram_store.get_file("tg_111", "chart") is None
    assert await telegram_store.delete_file("tg_111", "chart") is False


@pytest.mark.asyncio
async def test_list_files(isolated_db):
    await telegram_store.put_file("tg_111", "a", "ID_A", "photo")
    await telegram_store.put_file("tg_111", "b", "ID_B", "document")
    await telegram_store.put_file("tg_222", "a", "ID_OTHER", "audio")

    own = await telegram_store.list_files("tg_111")
    assert [r["file_ref"] for r in own] == ["a", "b"]


@pytest.mark.asyncio
async def test_prune_expired_files(isolated_db):
    import aiosqlite as _aiosqlite

    await telegram_store.put_file("tg_111", "fresh", "ID_FRESH", "photo")
    await telegram_store.put_file("tg_111", "stale", "ID_STALE", "document")

    past = (
        datetime.now(timezone.utc) - timedelta(days=10)
    ).isoformat(timespec="microseconds")
    async with _aiosqlite.connect(str(isolated_db)) as conn:
        await conn.execute(
            "UPDATE outbound_files SET expires_at = ? WHERE file_ref = ?",
            (past, "stale"),
        )
        await conn.commit()

    deleted = await telegram_store.prune_expired_files()
    assert deleted == 1
    fresh_present = await telegram_store.get_file(
        "tg_111", "fresh", slide=False
    )
    assert fresh_present is not None


# --------------------------------------------------------------------------- last-forward cache


def test_last_forward_stash_peek_pop(isolated_db):
    telegram_store.stash_forward(
        "tg_111",
        chat_id=-1001234,
        chat_type="supergroup",
        title="Eng Team",
        username="eng_chat",
    )
    peeked = telegram_store.peek_forward("tg_111")
    assert peeked is not None
    assert peeked.chat_id == -1001234
    # Peek does not consume.
    again = telegram_store.peek_forward("tg_111")
    assert again is not None

    popped = telegram_store.pop_forward("tg_111")
    assert popped is not None
    assert popped.chat_id == -1001234
    # Pop consumes.
    assert telegram_store.peek_forward("tg_111") is None
    assert telegram_store.pop_forward("tg_111") is None


def test_last_forward_ttl_expires(isolated_db, monkeypatch):
    telegram_store.stash_forward(
        "tg_111", chat_id=-100, chat_type="supergroup"
    )
    # Force entry's captured_at into the past beyond the TTL.
    entry = telegram_store._last_forward_cache["tg_111"]
    entry.captured_at = datetime.now(timezone.utc) - timedelta(seconds=600)

    # peek should expire-and-clear.
    assert telegram_store.peek_forward("tg_111") is None
    assert "tg_111" not in telegram_store._last_forward_cache


def test_last_forward_no_session_no_op(isolated_db):
    telegram_store.stash_forward(
        "", chat_id=-100, chat_type="supergroup"
    )
    assert telegram_store._last_forward_cache == {}
    assert telegram_store.peek_forward("") is None
    assert telegram_store.pop_forward("") is None
