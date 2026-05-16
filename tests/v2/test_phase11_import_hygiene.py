"""Phase-11 import-hygiene pins (mirrors phase-9 / phase-10).

Per ``docs/PHASE_11_PLAN.md`` §5 + the claude-reviewer
slice-8 hard-checks. Every phase-11 NEW module must:

- NOT import ``app.v2.runtime._defaults`` at module scope
  (clock + id factories stay DI; ``_defaults`` is the SOLE
  ``uuid`` / ``datetime.now`` binding site).
- NOT import a heavy I/O SDK at module load (``slack_sdk``
  / ``google`` / ``googleapiclient`` / ``httpx`` /
  ``oauth2client`` / ``requests`` / ``urllib3`` /
  ``aiohttp``) — transports are protocol-typed DI
  (phase-6/9/10 carry-forward).
- NOT call ``uuid.uuid4`` / ``datetime.now`` ANYWHERE
  (function bodies + f-strings included).

The phase-11 NEW modules are exactly:
- ``app.v2.emit.source_post`` (slice 5)
- ``app.v2.templates.recurring_series_from_source`` (slice 6)

``app.v2.models.event`` / ``app.v2.runtime.worker`` /
``app.v2.sources.resolver`` / ``app.v2.authoring.templates``
were only EXTENDED in phase 11 (not new) — their hygiene is
pinned by the phase that created them and by their own
slices' empty-diff / behaviour pins; re-pinning them here
would be redundant and out of this slice's scope.

**Direct-name-import robustness (slice-5 tracked 🔵, landed
here).** The phase-10 call detector compared the raw
attribute chain, so it only caught the attribute forms
``uuid.uuid4(...)`` / ``datetime.now(...)``. It was blind
to the direct-name-import forms:

    from uuid import uuid4
    uuid4()                       # chain == ['uuid4']

    from datetime import datetime as dt
    dt.now()                      # chain == ['dt', 'now']

This pin resolves every call's leftmost name through an
import-alias map (built from EVERY ``import`` /
``from ... import`` in the tree, including lazy in-function
imports — calling ``uuid4`` / ``datetime.now`` lazily is
still forbidden), canonicalises it, and matches against the
canonical forbidden set. Both alias and attribute forms are
caught.

A lazy import inside a function body is allowed for heavy
SDKs (the sanctioned pattern) — the module-LOAD collector
does not descend into function / class bodies. The CALL
detector DOES (a uuid/datetime.now call is forbidden
anywhere, lazily or not).
"""

from __future__ import annotations

import ast
import importlib
import inspect

import pytest


_PHASE11_MODULES = [
    "app.v2.emit.source_post",
    "app.v2.templates.recurring_series_from_source",
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
# After alias resolution, ``from uuid import uuid4; uuid4()``
# canonicalises to ``uuid.uuid4`` and both
# ``from datetime import datetime; datetime.now()`` and
# ``from datetime import datetime as dt; dt.now()``
# canonicalise to ``datetime.datetime.now`` — while the bare
# ``import datetime; datetime.now()`` stays ``datetime.now``.
# All three forbidden canonicalisations are listed.
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
# Module-LOAD import collector (module scope only) — phase-10 parity
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
    AND function / class bodies — a lazy ``from uuid import
    uuid4`` then ``uuid4()`` is still forbidden).

    - ``import uuid``               → {"uuid": "uuid"}
    - ``import uuid as u``          → {"u": "uuid"}
    - ``import a.b.c``              → {"a": "a"}  (top bind)
    - ``from uuid import uuid4``    → {"uuid4": "uuid.uuid4"}
    - ``from uuid import uuid4 as u``
                                    → {"u": "uuid.uuid4"}
    - ``from datetime import datetime``
                                    → {"datetime":
                                       "datetime.datetime"}
    - ``from datetime import datetime as dt``
                                    → {"dt":
                                       "datetime.datetime"}
    """
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
            # Skip relative imports (level > 0) and bare
            # ``from . import x`` (module is None).
            if node.level or not node.module:
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                amap[bound] = f"{node.module}.{alias.name}"
    return amap


def _call_chain(node: ast.Call) -> list[str]:
    """Leftmost Name + the Attribute chain, e.g.
    ``datetime.datetime.now(...)`` → ['datetime',
    'datetime', 'now']. Empty if the callee root is not a
    plain Name (e.g. a subscript / call result)."""
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


@pytest.mark.parametrize("module_name", _PHASE11_MODULES)
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


@pytest.mark.parametrize("module_name", _PHASE11_MODULES)
def test_no_forbidden_runtime_call(module_name):
    hits = _module_has_forbidden_call(module_name)
    assert not hits, (
        f"{module_name}: forbidden call(s) {hits!r}; clock "
        f"/ id factories are DI (only app.v2.runtime."
        f"_defaults binds uuid / datetime.now). This pin "
        f"resolves import aliases, so 'from uuid import "
        f"uuid4; uuid4()' is caught too."
    )


# --- helper self-tests (mirror phase-9 / phase-10) ------------------------


def test_call_detector_sees_defaults_own_bindings():
    """The resolver MUST flag _defaults (its binding site:
    ``import uuid; uuid.uuid4()`` and ``from datetime
    import datetime; datetime.now(...)``) — else the forbid
    pins are vacuous."""
    hits = _module_has_forbidden_call("app.v2.runtime._defaults")
    assert "uuid.uuid4" in hits
    assert "datetime.datetime.now" in hits


def test_resolver_catches_direct_name_import_uuid4():
    """``from uuid import uuid4`` then bare ``uuid4()`` — the
    form the phase-10 detector was blind to (chain ==
    ['uuid4']). Must canonicalise to ``uuid.uuid4``."""
    src = "from uuid import uuid4\n\n\ndef f():\n    return uuid4()\n"
    assert _forbidden_calls_in_src(src) == ["uuid.uuid4"]


def test_resolver_catches_aliased_datetime_now():
    """``from datetime import datetime as dt`` then
    ``dt.now()`` (chain == ['dt', 'now']) — must
    canonicalise to ``datetime.datetime.now``."""
    src = (
        "from datetime import datetime as dt\n\n\n"
        "def f():\n    return dt.now()\n"
    )
    assert _forbidden_calls_in_src(src) == ["datetime.datetime.now"]


def test_resolver_catches_aliased_uuid4_call():
    """``from uuid import uuid4 as u`` then ``u()``."""
    src = "from uuid import uuid4 as u\nx = u()\n"
    assert _forbidden_calls_in_src(src) == ["uuid.uuid4"]


def test_resolver_catches_lazy_in_function_import():
    """A lazy in-FUNCTION ``from uuid import uuid4`` then
    ``uuid4()`` is still forbidden — the CALL detector walks
    function bodies (only the module-LOAD collector stops at
    the function boundary)."""
    src = (
        "def f():\n"
        "    from uuid import uuid4\n"
        "    return uuid4().hex\n"
    )
    assert _forbidden_calls_in_src(src) == ["uuid.uuid4"]


def test_resolver_does_not_flag_unrelated_now_or_utcnow():
    """Negative control — the pin must not be vacuously
    over-broad. A local ``now()`` with no uuid/datetime
    binding, and ``datetime.utcnow()`` (NOT ``.now``), are
    both clean."""
    clean = (
        "from datetime import datetime as dt\n"
        "def now():\n    return 1\n"
        "a = now()\n"
        "b = dt.utcnow()\n"
    )
    assert _forbidden_calls_in_src(clean) == []


def test_scope_collector_sees_a_real_module_scope_import():
    """recurring_series_from_source imports CronTrigger at
    module scope — assert the module-LOAD collector sees
    module-scope imports at all (forbid pin can't pass
    vacuously)."""
    imports = _module_scope_imports(
        "app.v2.templates.recurring_series_from_source"
    )
    assert "app.v2.models.triggers" in imports
