"""Phase-9 import-hygiene pins for the NEW phase-9 modules.

Slice-8 reviewer 🔴: ``app/v2/wiring.py`` violated the
phase-9 hard rule by (a) importing ``app.v2.runtime._defaults``
at module load and (b) calling ``uuid.uuid4()`` directly.
``_defaults`` is the SOLE runtime ``uuid`` / ``datetime.now``
binding site; every phase-9 NEW module must keep clear of
both.

This module pins the rule so a regression flips loud:

- **No module-load ``app.v2.runtime._defaults`` import.** A
  *lazy* import inside a function body is explicitly allowed
  (that is the slice-8 fix shape for
  :func:`app.v2.wiring.build_authoring_toolset`), so the AST
  walk deliberately does NOT descend into function / class
  bodies — it only flags imports at MODULE scope (including
  module-level ``try`` / ``if`` blocks).
- **No ``uuid.uuid4`` / ``datetime.now`` call anywhere** in
  the module (function bodies included — production code in
  these modules must never bind the clock / id stream; it
  delegates to ``_defaults``). f-string-embedded calls are
  caught (``ast.walk`` recurses ``JoinedStr``).

Mirrors the existing helpers:
- ``tests/v2/test_runtime_defaults.py`` ``_function_body_calls``
  (AST ``ast.Call`` attribute-chain walk, docstring-safe).
- ``tests/v2/test_registry_cache_smoke.py`` ``_module_imports``
  + ``_FORBIDDEN_RUNTIME`` / ``_matches_any``.
"""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest


# The phase-9 NEW modules. ``app.v2.runtime.boot`` is NOT
# here: it is a phase-5/7 *runtime* module and is allowed to
# bind ``_defaults`` (it lives in the runtime package and is
# the wiring caller). Only the genuinely new phase-9 seams
# are pinned.
_PHASE9_MODULES = [
    "app.v2.wiring",
    "app.v2.boot",
    "app.v2.transports",
    "app.v2.transports.slack",
]


_FORBIDDEN_RUNTIME = {
    "app.v2.runtime._defaults",
}

_FORBIDDEN_CALLS = (
    "uuid.uuid4",
    "datetime.now",
)


def _matches_any(imported: str, forbidden_set: set[str]) -> bool:
    """True if ``imported`` equals, or is a submodule of, any
    entry in ``forbidden_set`` (``a.b`` matches ``a``)."""
    return any(
        imported == bad or imported.startswith(bad + ".")
        for bad in forbidden_set
    )


class _ModuleScopeImportCollector(ast.NodeVisitor):
    """Collect imported dotted module names at MODULE scope
    only.

    Overrides the function / class / lambda visitors to NOT
    recurse, so a lazy import inside a function body (the
    sanctioned slice-8 ``_defaults`` access pattern) is
    invisible here. ``generic_visit`` still recurses
    module-level ``try`` / ``if`` / ``with`` blocks so a
    top-level guarded import is still caught.
    """

    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return  # do not descend into function bodies

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return  # do not descend into class bodies

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
    """True iff the module source contains a real call to
    ``dotted_name`` (e.g. ``uuid.uuid4``) ANYWHERE — function
    bodies and f-strings included. AST ``ast.Call`` walk so
    docstring / comment mentions never false-match."""
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


# ===========================================================================
# No module-load _defaults import
# ===========================================================================


@pytest.mark.parametrize("module_name", _PHASE9_MODULES)
def test_no_module_load_runtime_defaults_import(module_name):
    """Phase-9 NEW modules must NOT import
    ``app.v2.runtime._defaults`` at module load. A lazy
    import inside a function body is allowed (slice-8 fix
    shape) and is invisible to the module-scope collector."""
    leaked = [
        i
        for i in _module_scope_imports(module_name)
        if _matches_any(i, _FORBIDDEN_RUNTIME)
    ]
    assert not leaked, (
        f"{module_name}: module-load import of "
        f"runtime._defaults is forbidden by the phase-9 hard "
        f"rule (use a lazy import inside the function body "
        f"instead): {leaked!r}"
    )


def test_wiring_lazy_defaults_import_is_not_false_flagged():
    """Guard the guard: ``app.v2.wiring`` DOES import
    ``_defaults`` lazily inside ``build_authoring_toolset``.
    The module-scope collector must NOT see it (otherwise the
    pin above would be un-satisfiable and the slice-8 fix
    impossible). Pin the helper's scope semantics."""
    scope_imports = _module_scope_imports("app.v2.wiring")
    assert not any(
        _matches_any(i, _FORBIDDEN_RUNTIME) for i in scope_imports
    )
    # ... but the source DOES contain the lazy import (it is
    # just inside a function, not at module scope).
    src = inspect.getsource(importlib.import_module("app.v2.wiring"))
    assert "from app.v2.runtime._defaults import" in src, (
        "expected wiring.py to retain the LAZY _defaults "
        "import inside build_authoring_toolset"
    )


# ===========================================================================
# No uuid.uuid4 / datetime.now call anywhere
# ===========================================================================


@pytest.mark.parametrize("module_name", _PHASE9_MODULES)
@pytest.mark.parametrize("dotted", _FORBIDDEN_CALLS)
def test_no_forbidden_runtime_call(module_name, dotted):
    """Phase-9 NEW modules must not call ``uuid.uuid4`` /
    ``datetime.now`` — those bindings live only in
    ``app.v2.runtime._defaults``."""
    assert not _module_calls(module_name, dotted), (
        f"{module_name}: forbidden call to {dotted}(...); "
        f"the phase-9 hard rule routes uuid / wall-clock "
        f"through app.v2.runtime._defaults only"
    )


# ===========================================================================
# Self-tests for the AST helpers (mirror test_runtime_defaults
# helper self-tests so a helper regression flips here).
# ===========================================================================


def test_module_calls_helper_detects_defaults_own_uuid():
    """``app.v2.runtime._defaults`` IS the binding site — the
    call-detection helper must see ``uuid.uuid4`` there.
    Inverse smoke: if this flips False, the helper went
    blind and the forbid pins above are vacuous."""
    assert _module_calls("app.v2.runtime._defaults", "uuid.uuid4")
    assert _module_calls("app.v2.runtime._defaults", "datetime.now")


def test_module_scope_collector_sees_top_level_import():
    """The collector MUST catch a genuine module-scope
    import. ``app.v2.wiring`` imports
    ``app.v2.runtime._owner_default`` at module load (the
    documented env exception, NOT forbidden) — assert the
    collector sees module-scope imports at all so the
    forbid pin can't pass vacuously."""
    scope_imports = _module_scope_imports("app.v2.wiring")
    assert "app.v2.runtime._owner_default" in scope_imports
