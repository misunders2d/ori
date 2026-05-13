"""On-disk persistence for contracts — versioned, hash-verified.

Layout under ``data/contracts/``::

    data/contracts/
      <contract_id>/
        index.json                 ← list of versions, latest first
        v1__<hash[:12]>.json       ← frozen body
        v2__<hash[:12]>.json
        ...

Each frozen body is a one-shot write — never edited in place. A revise
operation produces a NEW file with a NEW hash; the previous version
stays around for audit. Hash mismatches on load raise
``ContractHashMismatch`` and abort the fire (defends against on-disk
tampering or a botched manual edit).

The store is intentionally simple JSON-on-disk: no SQLite, no Neo4j.
Contracts are low-rate write, high-rate read; the filesystem layout
makes diffing two versions trivial (just ``diff vN.json vM.json``).
"""

from __future__ import annotations

import json
import os
import threading
from typing import Optional

from app.contracts.schema import Contract, ContractVersion


class ContractError(Exception):
    """Base for all contract-store errors."""


class ContractNotFound(ContractError):
    """No contract with the requested id exists."""


class ContractHashMismatch(ContractError):
    """The stored body's hash does not match the filename / index — the
    contract has been tampered with (or a write race produced a corrupt
    file). The executor refuses to fire under these conditions."""


class ContractStore:
    """JSON-on-disk contract store with version chain + hash verify.

    Thread-safe per-contract via an in-process lock dict; cross-process
    safety relies on atomic ``os.replace``. The supervisor and the bot
    run in the same process so a single lock is sufficient.
    """

    def __init__(self, root: Optional[str] = None):
        self._root = os.path.abspath(root or "./data/contracts")
        self._locks: dict[str, threading.Lock] = {}
        self._locks_master = threading.Lock()

    # ----- paths -----

    def _contract_dir(self, contract_id: str) -> str:
        return os.path.join(self._root, contract_id)

    def _index_path(self, contract_id: str) -> str:
        return os.path.join(self._contract_dir(contract_id), "index.json")

    def _version_path(self, contract_id: str, version: int, hash_: str) -> str:
        return os.path.join(
            self._contract_dir(contract_id), f"v{version}__{hash_[:12]}.json"
        )

    def _lock_for(self, contract_id: str) -> threading.Lock:
        with self._locks_master:
            lock = self._locks.get(contract_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[contract_id] = lock
            return lock

    # ----- index -----

    def _read_index(self, contract_id: str) -> list[ContractVersion]:
        path = self._index_path(contract_id)
        if not os.path.isfile(path):
            return []
        with open(path) as f:
            raw = json.load(f) or []
        return [ContractVersion(**r) for r in raw]

    def _write_index(self, contract_id: str, versions: list[ContractVersion]) -> None:
        os.makedirs(self._contract_dir(contract_id), exist_ok=True)
        path = self._index_path(contract_id)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump([v.model_dump(mode="json") for v in versions], f, indent=2)
        os.replace(tmp, path)

    # ----- write -----

    def freeze(self, contract: Contract) -> Contract:
        """Compute the hash, persist the version file, update the index.

        Returns a frozen copy of ``contract`` with the ``hash`` field
        populated.

        Refuses to re-freeze an unchanged contract — the caller should
        catch ``ValueError`` if they're calling speculatively. Two
        successive freezes of the same canonical body produce identical
        hashes; the second freeze is a no-op that returns the existing
        record.

        Refuses to freeze a contract that references unknown adapters /
        gates / loaders. Defense-in-depth duplicate of the check in
        ``app.tools.contracts._coerce_spec`` — covers direct-API callers
        that bypass the tools layer (tests, scripts, future REST entry).
        """
        # Lazy import to break the schema → emit → store cycle.
        from app.contracts.validation import (
            validate_adapter_arg_shapes,
            validate_against_registries,
            validate_step_rigor,
        )
        validate_against_registries(contract)
        validate_step_rigor(contract)
        validate_adapter_arg_shapes(contract)

        frozen = contract.with_fresh_hash()

        with self._lock_for(frozen.id):
            versions = self._read_index(frozen.id)

            # Idempotent: same hash already on disk → return the existing
            # record. Lets the AUTHOR flow be safely retried.
            for v in versions:
                if v.hash == frozen.hash:
                    return frozen

            # The new freeze must follow the latest version. If the
            # caller passed a stale ``version`` field, advance it.
            next_version = (versions[0].version + 1) if versions else 1
            if frozen.version != next_version:
                frozen = frozen.model_copy(update={"version": next_version})
                # Recompute hash with the updated version field.
                frozen = frozen.with_fresh_hash()

            # Persist body file.
            os.makedirs(self._contract_dir(frozen.id), exist_ok=True)
            body_path = self._version_path(frozen.id, frozen.version, frozen.hash)
            tmp = f"{body_path}.tmp"
            with open(tmp, "w") as f:
                json.dump(frozen.model_dump(mode="json"), f, indent=2)
            os.replace(tmp, body_path)

            # Prepend new version to the index (latest-first ordering
            # makes ``load_latest`` trivial).
            new_entry = ContractVersion(
                version=frozen.version,
                hash=frozen.hash,
                authored_at=frozen.authored_at,
                author=frozen.author,
                parent_hash=frozen.parent_hash,
            )
            self._write_index(frozen.id, [new_entry, *versions])

        return frozen

    # ----- read -----

    def list_versions(self, contract_id: str) -> list[ContractVersion]:
        """Return all known versions for a contract, latest first."""
        return self._read_index(contract_id)

    def load_latest(self, contract_id: str) -> Contract:
        """Load the most recently frozen version of a contract."""
        versions = self._read_index(contract_id)
        if not versions:
            raise ContractNotFound(f"no contract with id={contract_id!r}")
        latest = versions[0]
        return self.load(contract_id, latest.hash)

    def load(self, contract_id: str, hash_: str) -> Contract:
        """Load a specific version by hash, verifying integrity.

        Raises ``ContractHashMismatch`` if the on-disk body no longer
        hashes to the expected value — defence against manual edits or
        partial-write corruption.
        """
        versions = self._read_index(contract_id)
        version_entry = next((v for v in versions if v.hash == hash_), None)
        if version_entry is None:
            raise ContractNotFound(
                f"no version with hash={hash_!r} for contract {contract_id!r}"
            )

        path = self._version_path(contract_id, version_entry.version, hash_)
        if not os.path.isfile(path):
            raise ContractNotFound(
                f"index references missing body file: {path}"
            )

        with open(path) as f:
            raw = json.load(f)
        contract = Contract.model_validate(raw)

        # Integrity check: the body must hash to the value the index
        # claims. If it doesn't, something rewrote the file — abort.
        recomputed = contract.compute_hash()
        if recomputed != hash_:
            raise ContractHashMismatch(
                f"contract {contract_id!r} v{version_entry.version}: "
                f"stored hash {hash_!r} does not match recomputed "
                f"{recomputed!r}. The body has been tampered with — "
                f"refusing to execute."
            )

        return contract

    def list_all(self) -> list[str]:
        """Return all known contract ids."""
        if not os.path.isdir(self._root):
            return []
        out = []
        for name in sorted(os.listdir(self._root)):
            if os.path.isfile(os.path.join(self._root, name, "index.json")):
                out.append(name)
        return out


# Module-level singleton used by the rest of the package. Tests get
# their own ``ContractStore(tmp_path)`` instance — see
# ``tests/test_contracts_store.py``.
contract_store = ContractStore()
