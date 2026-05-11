"""On-disk store tests — freeze, load, version chain, hash verify,
tamper detection.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.contracts.schema import (
    Contract,
    CronTrigger,
    EmitStep,
)
from app.contracts.store import (
    ContractHashMismatch,
    ContractNotFound,
    ContractStore,
)


def _minimal_contract(**overrides) -> Contract:
    base = dict(
        id="daily_tip",
        description="Daily AI tip post.",
        author="sergey",
        trigger=CronTrigger(cron="0 18 * * MON-FRI", timezone="Europe/Kyiv"),
        emit=[
            EmitStep(
                adapter="slack_post",
                args={"channel": "#ai_in_mellanni", "content": "Hello."},
            )
        ],
    )
    base.update(overrides)
    return Contract(**base)


@pytest.fixture
def store(tmp_path) -> ContractStore:
    return ContractStore(root=str(tmp_path / "contracts"))


# ---------------------------------------------------------------------------
# Freeze + load round-trip
# ---------------------------------------------------------------------------


def test_freeze_then_load_latest(store: ContractStore):
    """The happy path: freeze a contract, load it back via
    ``load_latest``, content matches."""
    c = _minimal_contract()
    frozen = store.freeze(c)
    assert frozen.hash != ""
    assert frozen.version == 1

    loaded = store.load_latest(c.id)
    assert loaded.hash == frozen.hash
    assert loaded.description == c.description
    assert loaded.version == 1


def test_freeze_writes_index_with_latest_first(store: ContractStore):
    """Two successive freezes (revisions) produce a chain — newest at
    index[0]."""
    v1 = store.freeze(_minimal_contract())
    v2 = store.freeze(_minimal_contract(description="Different desc"))

    versions = store.list_versions("daily_tip")
    assert [v.version for v in versions] == [2, 1]
    assert versions[0].hash == v2.hash
    assert versions[1].hash == v1.hash


def test_freeze_is_idempotent_for_unchanged_body(store: ContractStore):
    """Re-freezing the same canonical body returns the existing record
    rather than creating a duplicate version. Authoring flow can retry
    safely."""
    c = _minimal_contract()
    a = store.freeze(c)
    b = store.freeze(c)
    assert a.hash == b.hash
    assert a.version == b.version == 1
    assert len(store.list_versions(c.id)) == 1


def test_freeze_auto_advances_version_on_meaningful_change(store: ContractStore):
    """If the caller forgets to bump ``version`` after a content edit,
    the store does it for them — the chain stays consecutive."""
    store.freeze(_minimal_contract())
    edited = _minimal_contract(description="changed")  # version still 1
    assert edited.version == 1

    frozen = store.freeze(edited)
    assert frozen.version == 2


def test_load_specific_version_by_hash(store: ContractStore):
    v1 = store.freeze(_minimal_contract())
    store.freeze(_minimal_contract(description="changed"))

    # Old version still loadable by its hash — audit trail intact.
    loaded = store.load("daily_tip", v1.hash)
    assert loaded.version == 1
    assert loaded.description == "Daily AI tip post."


# ---------------------------------------------------------------------------
# Missing / not-found
# ---------------------------------------------------------------------------


def test_load_latest_raises_on_unknown_id(store: ContractStore):
    with pytest.raises(ContractNotFound):
        store.load_latest("ghost")


def test_load_by_hash_raises_on_unknown_hash(store: ContractStore):
    store.freeze(_minimal_contract())
    with pytest.raises(ContractNotFound):
        store.load("daily_tip", "0" * 64)


# ---------------------------------------------------------------------------
# Tamper detection — load() must refuse mutated bodies
# ---------------------------------------------------------------------------


def test_load_raises_on_post_freeze_tampering(store: ContractStore, tmp_path):
    """A manual edit of an on-disk body file changes its hash but the
    filename still claims the old hash. ``load`` must catch this and
    refuse to execute — defending the system against silent contract
    rewrites (accidental or hostile)."""
    frozen = store.freeze(_minimal_contract())

    # Locate the body file and mutate it.
    body_path = pathlib.Path(
        store._version_path("daily_tip", frozen.version, frozen.hash)
    )
    body = json.loads(body_path.read_text())
    body["description"] = "TAMPERED — was not authored, was inserted."
    body_path.write_text(json.dumps(body))

    with pytest.raises(ContractHashMismatch, match="tampered with"):
        store.load("daily_tip", frozen.hash)


def test_load_raises_when_body_file_deleted(store: ContractStore):
    """Index points to a file but the file vanished (manual ``rm``,
    half-deleted backup, etc) — ``load`` reports a clear error rather
    than silently returning a default."""
    frozen = store.freeze(_minimal_contract())
    pathlib.Path(
        store._version_path("daily_tip", frozen.version, frozen.hash)
    ).unlink()

    with pytest.raises(ContractNotFound, match="missing body file"):
        store.load("daily_tip", frozen.hash)


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------


def test_list_all_returns_known_ids_alphabetical(store: ContractStore):
    store.freeze(_minimal_contract(id="zebra"))
    store.freeze(_minimal_contract(id="alpha"))
    store.freeze(_minimal_contract(id="mango"))

    assert store.list_all() == ["alpha", "mango", "zebra"]


def test_list_all_empty_when_no_contracts(store: ContractStore):
    assert store.list_all() == []


# ---------------------------------------------------------------------------
# Parent-hash chain
# ---------------------------------------------------------------------------


def test_revision_records_parent_hash(store: ContractStore):
    """Manually-authored revisions set ``parent_hash`` to the previous
    version they were edited from. The store preserves this in the
    index for audit / diff tooling."""
    v1 = store.freeze(_minimal_contract())
    revised = _minimal_contract(description="changed", parent_hash=v1.hash)
    v2 = store.freeze(revised)

    versions = store.list_versions("daily_tip")
    assert versions[0].parent_hash == v1.hash
    assert versions[1].parent_hash is None
