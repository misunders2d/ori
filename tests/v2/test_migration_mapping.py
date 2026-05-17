"""Phase 16 slice 1 — pure v1→v2 migration mapping + dry-run.

Per ``docs/PHASE_16_PLAN.md`` §1/§3/§5 + the claude-reviewer
round-1 disposition (Q1–Q7) + the GO-slice-1 binding
criteria.

Pins: assess fidelity + honest per-reason SKIP (no silent
coercion) + faithful map of the migratable subset + dry-run
enumeration + the v1 READ-ONLY import pin (the migration
package imports NO v1 write / executor / tasks /
scheduler_instance path — alias-robust AST scan) + a folded
alias-robust AST import-hygiene pin (no module-load
datetime.now / uuid4 / vendor-SDK). Slice 1 is PURE: no v2
DB conn, ZERO v2 write, ZERO v1 mutation (the mapping /
dry-run functions take no connection).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.contracts.schema import (
    Contract,
    CronTrigger,
    EmitStep,
    EventTrigger,
    FailureAction,
    OnDemandTrigger,
)
from app.v2.migration import (
    ContractNotMigratable,
    MigrationBinding,
    MigrationStatus,
    assess_contract,
    contract_to_schedule_spec,
    migration_dry_run,
)


def _contract(**over) -> Contract:
    base = dict(
        id="daily_digest",
        description="Daily Amazon sales digest summary.",
        author="U_AUTHOR",
        trigger=CronTrigger(cron="0 9 * * *", timezone="UTC"),
        emit=[EmitStep(adapter="slack_post", args={"channel": "C1"})],
    )
    base.update(over)
    return Contract(**base)


_BINDING = MigrationBinding(
    platform="slack", target_session_id="C012ABCDE"
)


# ---------------------------------------------------------------------------
# assess — migratable
# ---------------------------------------------------------------------------


def test_assess_simple_cron_is_migratable():
    a = assess_contract(_contract())
    assert a.status is MigrationStatus.MIGRATABLE_WITH_BINDING
    assert a.skip_reasons == []
    assert a.required_bindings == [
        "owner.platform",
        "delivery.target_session_id",
    ]


# ---------------------------------------------------------------------------
# assess — honest SKIP per structural reason (no silent coercion)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "over, needle",
    [
        (dict(trigger=OnDemandTrigger()), "no faithful v2 analogue"),
        (
            dict(trigger=EventTrigger(event="x")),
            "no faithful v2 analogue",
        ),
        (
            dict(emit=[
                EmitStep(adapter="slack_post", args={}),
                EmitStep(adapter="email", args={}),
            ]),
            "multi-emit",
        ),
        (
            dict(emit=[EmitStep(
                adapter="slack_post", args={},
                abort_on_gate_fail=True,
            )]),
            "abort_on_gate_fail",
        ),
        (dict(description="short"), "shorter than the v2 minimum"),
        (
            dict(on_failure=FailureAction(notify=["U_X"])),
            "non-default v1 on_failure",
        ),
    ],
)
def test_assess_skips_with_honest_reason(over, needle):
    a = assess_contract(_contract(**over))
    assert a.status is MigrationStatus.SKIPPED
    assert any(needle in r for r in a.skip_reasons), a.skip_reasons


def test_assess_collects_all_reasons_not_first_fail():
    a = assess_contract(
        _contract(
            trigger=OnDemandTrigger(),
            description="x",
        )
    )
    assert a.status is MigrationStatus.SKIPPED
    # Both the trigger AND the description reason present.
    assert any("v2 analogue" in r for r in a.skip_reasons)
    assert any("minimum" in r for r in a.skip_reasons)
    assert len(a.skip_reasons) >= 2


# ---------------------------------------------------------------------------
# contract_to_schedule_spec — faithful map / refuse on skipped
# ---------------------------------------------------------------------------


def test_map_faithful_and_unfrozen():
    spec = contract_to_schedule_spec(_contract(), binding=_BINDING)
    assert spec.id == "daily_digest"
    assert spec.owner.platform == "slack"
    assert spec.owner.user_id == "U_AUTHOR"  # binding.user_id None → author
    assert spec.description == "Daily Amazon sales digest summary."
    assert spec.trigger.type == "cron"
    assert spec.trigger.cron == "0 9 * * *"
    assert spec.delivery.target_session_id == "C012ABCDE"
    # Slice-1 is PURE: the spec is UNFROZEN — no with_fresh_hash,
    # no write.
    assert spec.hash == ""


def test_map_user_id_override():
    spec = contract_to_schedule_spec(
        _contract(),
        binding=MigrationBinding(
            platform="telegram",
            user_id="123",
            target_session_id="chat",
        ),
    )
    assert spec.owner.user_id == "123"


def test_map_refuses_skipped_contract():
    with pytest.raises(ContractNotMigratable):
        contract_to_schedule_spec(
            _contract(trigger=OnDemandTrigger()), binding=_BINDING
        )


# ---------------------------------------------------------------------------
# dry-run — honest enumeration over a stub ContractStore (ZERO disk,
# ZERO v1 write, ZERO v2 write — slice-1 is pure)
# ---------------------------------------------------------------------------


class _StubStore:
    """Exposes ONLY the two pure-read methods the dry-run uses
    (list_all / load_latest) — no disk, no v1 write."""

    def __init__(self, contracts: dict[str, Contract]):
        self._c = contracts

    def list_all(self) -> list[str]:
        return sorted(self._c)

    def load_latest(self, contract_id: str) -> Contract:
        return self._c[contract_id]


def test_dry_run_enumerates_migratable_and_skipped():
    store = _StubStore(
        {
            "ok_one": _contract(id="ok_one"),
            "skip_one": _contract(
                id="skip_one", trigger=OnDemandTrigger()
            ),
        }
    )
    report = migration_dry_run(store)
    assert report.migratable_count == 1
    assert report.skipped_count == 1
    by_id = {e.contract_id: e for e in report.entries}
    assert (
        by_id["ok_one"].status
        is MigrationStatus.MIGRATABLE_WITH_BINDING
    )
    assert by_id["skip_one"].status is MigrationStatus.SKIPPED
    assert by_id["skip_one"].skip_reasons  # honest reason present


# ---------------------------------------------------------------------------
# v1 READ-ONLY import pin — the migration package imports NO v1
# write / executor / tasks / scheduler_instance path (alias-robust
# AST scan, not literal grep) + no module-load nondeterminism
# ---------------------------------------------------------------------------


def test_migration_pkg_v1_read_only_and_hygiene():
    pkg = pathlib.Path("app/v2/migration")
    forbidden_v1 = {
        "app.contracts.executor",
        "app.tasks",
        "app.scheduler_instance",
    }
    banned_calls = {"now", "utcnow", "uuid4", "uuid1"}
    bad_imports: list[str] = []
    bad_calls: list[str] = []
    for py in sorted(pkg.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module in forbidden_v1 or node.module.startswith(
                    ("app.contracts.executor", "app.tasks",
                     "app.scheduler_instance")
                ):
                    bad_imports.append(f"{py.name}: {node.module}")
            if isinstance(node, ast.Import):
                for n in node.names:
                    if n.name in forbidden_v1:
                        bad_imports.append(f"{py.name}: {n.name}")
        # module-scope nondeterminism
        for top in tree.body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
                continue
            for sub in ast.walk(top):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    nm = (
                        f.attr if isinstance(f, ast.Attribute)
                        else f.id if isinstance(f, ast.Name)
                        else None
                    )
                    if nm in banned_calls:
                        bad_calls.append(f"{py.name}: {nm}()")
    assert bad_imports == [], f"v1 write/exec import: {bad_imports}"
    assert bad_calls == [], f"module-load nondeterminism: {bad_calls}"
