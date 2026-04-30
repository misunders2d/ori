"""Unit tests for app.runtime.roster — auto-populated user registry."""

from __future__ import annotations

import json

import pytest

from app.runtime import roster


@pytest.fixture
def isolated_roster(tmp_path, monkeypatch):
    """Redirect ROSTER_PATH to a temp file so tests don't touch real data/."""
    p = tmp_path / "roster.json"
    monkeypatch.setattr(roster, "ROSTER_PATH", str(p))
    return p


def test_record_user_creates_entry(isolated_roster):
    roster.record_user(
        user_id="tg_111",
        platform="telegram",
        chat_id=111,
        first_name="Ruslan",
        last_name="Shostak",
        username="rshostak",
    )
    data = json.loads(isolated_roster.read_text())
    assert "tg_111" in data
    e = data["tg_111"]
    assert e["chat_id"] == 111
    assert e["display_name"] == "Ruslan Shostak"
    assert e["last_seen"]


def test_record_user_updates_existing(isolated_roster):
    roster.record_user("tg_111", "telegram", 111, first_name="Old")
    roster.record_user("tg_111", "telegram", 111, first_name="New", last_name="Name")
    data = json.loads(isolated_roster.read_text())
    assert data["tg_111"]["display_name"] == "New Name"


def test_display_name_falls_back_to_username(isolated_roster):
    roster.record_user("tg_222", "telegram", 222, username="solo_handle")
    data = json.loads(isolated_roster.read_text())
    assert data["tg_222"]["display_name"] == "solo_handle"


def test_display_name_falls_back_to_chat_id(isolated_roster):
    roster.record_user("tg_333", "telegram", 333)
    data = json.loads(isolated_roster.read_text())
    assert data["tg_333"]["display_name"] == "333"


def test_lookup_by_first_name(isolated_roster):
    roster.record_user("tg_111", "telegram", 111, first_name="Ruslan", last_name="Shostak")
    matches = roster.lookup_by_name("Ruslan")
    assert len(matches) == 1
    assert matches[0]["user_id"] == "tg_111"


def test_lookup_by_last_name(isolated_roster):
    roster.record_user("tg_111", "telegram", 111, first_name="Ruslan", last_name="Shostak")
    matches = roster.lookup_by_name("shostak")
    assert len(matches) == 1


def test_lookup_by_username(isolated_roster):
    roster.record_user("tg_111", "telegram", 111, username="rshostak")
    matches = roster.lookup_by_name("rshostak")
    assert len(matches) == 1


def test_lookup_strips_at_prefix(isolated_roster):
    roster.record_user("tg_111", "telegram", 111, username="rshostak")
    matches = roster.lookup_by_name("@rshostak")
    assert len(matches) == 1


def test_lookup_case_insensitive(isolated_roster):
    roster.record_user("tg_111", "telegram", 111, first_name="Ruslan")
    assert len(roster.lookup_by_name("ruslan")) == 1
    assert len(roster.lookup_by_name("RUSLAN")) == 1


def test_lookup_substring_match_returns_multiple(isolated_roster):
    roster.record_user("tg_a", "telegram", 1, first_name="Anna")
    roster.record_user("tg_b", "telegram", 2, first_name="Anastasia")
    matches = roster.lookup_by_name("an")
    assert len(matches) == 2


def test_lookup_filters_by_platform(isolated_roster):
    roster.record_user("tg_111", "telegram", 111, first_name="Ruslan")
    roster.record_user("sl_111", "slack", "U123", first_name="Ruslan")
    tg_matches = roster.lookup_by_name("Ruslan", platform="telegram")
    sl_matches = roster.lookup_by_name("Ruslan", platform="slack")
    assert len(tg_matches) == 1 and tg_matches[0]["user_id"] == "tg_111"
    assert len(sl_matches) == 1 and sl_matches[0]["user_id"] == "sl_111"


def test_lookup_empty_query_returns_empty(isolated_roster):
    roster.record_user("tg_111", "telegram", 111, first_name="Ruslan")
    assert roster.lookup_by_name("") == []
    assert roster.lookup_by_name("   ") == []


def test_lookup_no_match(isolated_roster):
    roster.record_user("tg_111", "telegram", 111, first_name="Ruslan")
    assert roster.lookup_by_name("Hans") == []


def test_lookup_handles_missing_file(isolated_roster):
    assert roster.lookup_by_name("anyone") == []


def test_get_entry(isolated_roster):
    roster.record_user("tg_111", "telegram", 111, first_name="Ruslan")
    entry = roster.get_entry("tg_111")
    assert entry is not None
    assert entry["first_name"] == "Ruslan"
    assert roster.get_entry("tg_999") is None


def test_load_handles_corrupt_file(isolated_roster):
    isolated_roster.write_text("not valid json {")
    assert roster.lookup_by_name("anything") == []
