"""Phase-13 import-hygiene pins (carries the phase-11 slice-8
alias-robust detector verbatim).

Per ``docs/PHASE_13_PLAN.md`` §1 / §6 + the claude-reviewer
slice-1 hard-checks. Every phase-12 NEW module must:

- NOT import ``app.v2.runtime._defaults`` at module scope
  (clock + id factories stay DI; ``_defaults`` is the SOLE
  ``uuid`` / ``datetime.now`` binding site).
- NOT import a heavy I/O SDK at module load (``slack_sdk``
  / ``google`` / ``googleapiclient`` / ``httpx`` /
  ``oauth2client`` / ``requests`` / ``urllib3`` /
  ``aiohttp``).
- NOT call ``uuid.uuid4`` / ``datetime.now`` ANYWHERE.

Phase-13 NEW modules (slice 1):
- ``app.v2.runtime.state`` — the §6.5 cross-fire state
  RUNTIME primitives (thin over the shipped phase-3 CAS).

``app.v2.runtime.worker`` is only EXTENDED in the later
phase-13 seam slice (not new) — their hygiene is
pinned by the phase that created them and by their own slices.

The call detector resolves every call's leftmost name through
an import-alias map built from EVERY import in the tree
(module + lazy in-function), so the direct-name-import forms
the original phase-10 raw-chain detector was blind to —
``from uuid import uuid4; uuid4()`` and
``from datetime import datetime as dt; dt.now()`` — are caught
(the phase-11 slice-8 hardening, carried here).
"""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest


_PHASE13_MODULES = [
    "app.v2.runtime.state",
]

_FORBIDDEN_MODULE_LOAD = {
    "app.v2.runtime._defaults",
    "slack_sdk",
    "google",
    "googleapiclient",
    "oauth2client",
    "httpx",
    "requests",
    "urllib3",
    "aiohttp",
}

# Canonical resolved dotted forms of the forbidden calls.
_FORBIDDEN_CALL_CANONICAL = {
    "uuid.uuid4",
    "datetime.now",
    "datetime.datetime.now",
}


def _matches_any(imported: str, forbidden_set: set[str]) -> bool:
    return any(
        imported == bad or imported.startswith(bad + ".")
        for bad in forbidden_set
    )


# ---------------------------------------------------------------------------
# Module-LOAD import collector (module scope only)
# ---------------------------------------------------------------------------


class _ModuleScopeImportCollector(ast.NodeVisitor):
    """Collect imported dotted module names at MODULE scope
    only (does not descend into function / class bodies — a
    lazy in-function import is the sanctioned pattern for
    heavy SDKs)."""

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self.names.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        self.names.add(node.module or "")


def _module_scope_imports_src(source: str) -> set[str]:
    collector = _ModuleScopeImportCollector()
    collector.visit(ast.parse(source))
    return collector.names


def _module_scope_imports(module_name: str) -> set[str]:
    mod = importlib.import_module(module_name)
    return _module_scope_imports_src(inspect.getsource(mod))


# ---------------------------------------------------------------------------
# CALL detector — alias-resolving, robust to direct-name-import forms
# ---------------------------------------------------------------------------


def _build_alias_map(tree: ast.AST) -> dict[str, str]:
    """Map every locally-bound name to the dotted origin it
    refers to, across EVERY import in the tree (module scope
    AND function / class bodies)."""
    amap: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    amap[alias.asname] = alias.name
                else:
                    top = alias.name.split(".")[0]
                    amap[top] = top
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                amap[bound] = f"{node.module}.{alias.name}"
    return amap


def _call_chain(node: ast.Call) -> list[str]:
    """Leftmost Name + the Attribute chain. Empty if the
    callee root is not a plain Name."""
    chain: list[str] = []
    f = node.func
    while isinstance(f, ast.Attribute):
        chain.insert(0, f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        chain.insert(0, f.id)
        return chain
    return []


def _resolve(chain: list[str], amap: dict[str, str]) -> str:
    """Canonicalise a call chain through the alias map."""
    if not chain:
        return ""
    root = chain[0]
    if root in amap:
        resolved = amap[root]
        tail = chain[1:]
        return resolved if not tail else resolved + "." + ".".join(tail)
    return ".".join(chain)


def _forbidden_calls_in_src(source: str) -> list[str]:
    tree = ast.parse(source)
    amap = _build_alias_map(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        canon = _resolve(_call_chain(node), amap)
        if canon in _FORBIDDEN_CALL_CANONICAL:
            hits.append(canon)
    return hits


def _module_has_forbidden_call(module_name: str) -> list[str]:
    mod = importlib.import_module(module_name)
    return _forbidden_calls_in_src(inspect.getsource(mod))


# ---------------------------------------------------------------------------
# The pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _PHASE13_MODULES)
def test_no_forbidden_module_load_import(module_name):
    leaked = [
        i
        for i in _module_scope_imports(module_name)
        if _matches_any(i, _FORBIDDEN_MODULE_LOAD)
    ]
    assert not leaked, (
        f"{module_name}: forbidden module-load import "
        f"(runtime._defaults / heavy I/O SDK — use lazy "
        f"in-fn import or protocol DI): {leaked!r}"
    )


@pytest.mark.parametrize("module_name", _PHASE13_MODULES)
def test_no_forbidden_runtime_call(module_name):
    hits = _module_has_forbidden_call(module_name)
    assert not hits, (
        f"{module_name}: forbidden call(s) {hits!r}; clock "
        f"/ id factories are DI (only app.v2.runtime."
        f"_defaults binds uuid / datetime.now). This pin "
        f"resolves import aliases, so 'from uuid import "
        f"uuid4; uuid4()' is caught too."
    )


# --- helper self-tests (carry phase-11 slice-8) ---------------------------


def test_call_detector_sees_defaults_own_bindings():
    """The resolver MUST flag _defaults (its binding site) —
    else the forbid pins are vacuous."""
    hits = _module_has_forbidden_call("app.v2.runtime._defaults")
    assert "uuid.uuid4" in hits
    assert "datetime.datetime.now" in hits


def test_resolver_catches_direct_name_import_uuid4():
    src = "from uuid import uuid4\n\n\ndef f():\n    return uuid4()\n"
    assert _forbidden_calls_in_src(src) == ["uuid.uuid4"]


def test_resolver_catches_aliased_datetime_now():
    src = (
        "from datetime import datetime as dt\n\n\n"
        "def f():\n    return dt.now()\n"
    )
    assert _forbidden_calls_in_src(src) == ["datetime.datetime.now"]


def test_resolver_catches_aliased_uuid4_call():
    src = "from uuid import uuid4 as u\nx = u()\n"
    assert _forbidden_calls_in_src(src) == ["uuid.uuid4"]


def test_resolver_catches_lazy_in_function_import():
    src = (
        "def f():\n"
        "    from uuid import uuid4\n"
        "    return uuid4().hex\n"
    )
    assert _forbidden_calls_in_src(src) == ["uuid.uuid4"]


def test_resolver_does_not_flag_unrelated_now_or_utcnow():
    """Negative control — the pin must not be vacuously
    over-broad."""
    clean = (
        "from datetime import datetime as dt\n"
        "def now():\n    return 1\n"
        "a = now()\n"
        "b = dt.utcnow()\n"
    )
    assert _forbidden_calls_in_src(clean) == []


def test_scope_collector_sees_a_real_module_scope_import():
    """runtime.state imports app.v2.storage.schedule_state at
    module scope — assert the module-LOAD collector sees
    module-scope imports at all (forbid pin can't pass
    vacuously)."""
    imports = _module_scope_imports("app.v2.runtime.state")
    assert "app.v2.storage.schedule_state" in imports
