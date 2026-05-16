"""Phase-10 import-hygiene pins (mirrors phase-9).

Per ``docs/PHASE_10_PLAN.md`` §6 / §10. Every phase-10 NEW
module must:
- NOT import ``app.v2.runtime._defaults`` at module scope
  (clock + id factories stay DI; ``_defaults`` is the SOLE
  ``uuid``/``datetime.now`` binding site).
- NOT import a heavy I/O SDK at module load (``slack_sdk``
  / ``google`` / ``googleapiclient`` / ``httpx`` /
  ``oauth2client`` / ``requests`` / ``urllib3`` /
  ``aiohttp``) — transports are protocol-typed DI
  (phase-6/9 carry-forward).
- NOT call ``uuid.uuid4`` / ``datetime.now`` ANYWHERE
  (function bodies + f-strings included).

A lazy import inside a function body is allowed (the
sanctioned pattern); the module-scope collector does not
descend into function / class bodies.
"""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest


_PHASE10_MODULES = [
    "app.v2.sources",
    "app.v2.sources.contract",
    "app.v2.sources.errors",
    "app.v2.sources.registry",
    "app.v2.sources.literal",
    "app.v2.sources.local_file",
    "app.v2.sources.slack_thread",
    "app.v2.sources.drive_file",
    "app.v2.sources.snapshot_writer",
    "app.v2.sources.cache",
    "app.v2.sources.resolver",
    "app.v2.models.source_ref",
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

_FORBIDDEN_CALLS = ("uuid.uuid4", "datetime.now")


def _matches_any(imported: str, forbidden_set: set[str]) -> bool:
    return any(
        imported == bad or imported.startswith(bad + ".")
        for bad in forbidden_set
    )


class _ModuleScopeImportCollector(ast.NodeVisitor):
    """Collect imported dotted module names at MODULE scope
    only (does not descend into function / class bodies — a
    lazy in-function import is the sanctioned pattern)."""

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


def _module_scope_imports(module_name: str) -> set[str]:
    mod = importlib.import_module(module_name)
    tree = ast.parse(inspect.getsource(mod))
    collector = _ModuleScopeImportCollector()
    collector.visit(tree)
    return collector.names


def _module_calls(module_name: str, dotted_name: str) -> bool:
    mod = importlib.import_module(module_name)
    tree = ast.parse(inspect.getsource(mod))
    target = dotted_name.split(".")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain: list[str] = []
        f = node.func
        while isinstance(f, ast.Attribute):
            chain.insert(0, f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            chain.insert(0, f.id)
        if chain == target:
            return True
    return False


@pytest.mark.parametrize("module_name", _PHASE10_MODULES)
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


@pytest.mark.parametrize("module_name", _PHASE10_MODULES)
@pytest.mark.parametrize("dotted", _FORBIDDEN_CALLS)
def test_no_forbidden_runtime_call(module_name, dotted):
    assert not _module_calls(module_name, dotted), (
        f"{module_name}: forbidden call {dotted}(...); "
        f"clock / id factories are DI (only "
        f"app.v2.runtime._defaults binds uuid/datetime.now)"
    )


# --- helper self-tests (mirror phase-9) -----------------------------------


def test_call_helper_detects_defaults_own_uuid():
    """The detector must see uuid.uuid4 / datetime.now in
    _defaults (its binding site) — else the forbid pins are
    vacuous."""
    assert _module_calls("app.v2.runtime._defaults", "uuid.uuid4")
    assert _module_calls("app.v2.runtime._defaults", "datetime.now")


def test_scope_collector_sees_a_real_module_scope_import():
    """app.v2.sources.resolver imports app.v2.sources.cache
    at module scope — assert the collector sees module-scope
    imports at all (forbid pin can't pass vacuously)."""
    imports = _module_scope_imports("app.v2.sources.resolver")
    assert "app.v2.sources.cache" in imports
