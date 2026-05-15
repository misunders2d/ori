"""Package smoke tests for ``app.v2.registry_cache``.

Phase 6 slice 5 per ``docs/PHASE_6_PLAN.md`` §5.7.

Pins:
- Every name in :data:`__all__` resolves to a real attribute
  on the package, callable / type / data as appropriate.
- The package and every submodule do NOT import I/O libs at
  module load (``slack_sdk`` / ``googleapiclient`` /
  ``requests`` / ``httpx`` / ``urllib3`` / ``aiohttp`` /
  ``smtplib`` / ``subprocess``). Carried from phases 4-5.
- The package and every submodule do NOT import
  ``app.v2.runtime._defaults`` at module load (round-2
  reviewer L347).
- No public callable in any phase-6 module has a name
  suggesting reasoning / emit / sub-agents / delegate /
  transfer / fire / claim / execute (carried from phase 4-5).
- ``app/v2/models/common.py`` no longer references
  ``phase-3`` inside :class:`ChannelRef` /
  :class:`SheetRef` docstrings.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.v2 import registry_cache as pkg


# ===========================================================================
# __all__ surface
# ===========================================================================


_EXPECTED_ALL = {
    "CacheFile",
    "CacheKind",
    "CacheMiss",
    "ChannelAmbiguous",
    "DEFAULT_CACHE_BASE",
    "GoogleDocsCache",
    "GoogleDocsEntry",
    "GoogleDriveClient",
    "GoogleSheetsCache",
    "GoogleSheetsEntry",
    "NoCacheAndNetworkDown",
    "NoCacheAvailable",
    "RegistryCacheError",
    "SlackChannelEntry",
    "SlackChannelsCache",
    "SlackChannelsClient",
    "WorkspaceMismatch",
    "cache_path",
    "is_stale",
    "load_cache",
    "refresh_google_docs",
    "refresh_google_sheets",
    "refresh_slack_channels",
    "resolve_channel",
    "resolve_doc",
    "resolve_sheet",
    "save_cache",
}


def test_all_lists_exactly_the_expected_surface():
    assert set(pkg.__all__) == _EXPECTED_ALL


def test_every_all_name_is_bound_on_package():
    for name in pkg.__all__:
        assert hasattr(pkg, name), (
            f"{name!r} listed in __all__ but not bound on package"
        )


# ===========================================================================
# Module-level import hygiene
# ===========================================================================


_FORBIDDEN_IO_LIBS = {
    "slack_sdk",
    "googleapiclient",
    "requests",
    "httpx",
    "urllib3",
    "aiohttp",
    "smtplib",
    "subprocess",
}

_FORBIDDEN_RUNTIME = {
    "app.v2.runtime._defaults",
}


_PHASE6_MODULES = [
    "app.v2.registry_cache",
    "app.v2.registry_cache.errors",
    "app.v2.registry_cache.schemas",
    "app.v2.registry_cache.paths",
    "app.v2.registry_cache.loader",
    "app.v2.registry_cache.refresh",
    "app.v2.registry_cache.resolver",
]


def _module_imports(module_name: str) -> set[str]:
    """AST walk: return every imported module name from the
    given dotted module path."""
    import importlib

    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod)
    tree = ast.parse(src)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def _matches_any(imported: str, forbidden_set: set[str]) -> bool:
    """Return True if ``imported`` matches any prefix in
    ``forbidden_set``. ``googleapiclient.discovery`` matches
    ``googleapiclient``."""
    return any(
        imported == bad or imported.startswith(bad + ".")
        for bad in forbidden_set
    )


def test_no_phase6_module_imports_io_libs():
    leaked: dict[str, list[str]] = {}
    for mod_name in _PHASE6_MODULES:
        bad = [
            i for i in _module_imports(mod_name)
            if _matches_any(i, _FORBIDDEN_IO_LIBS)
        ]
        if bad:
            leaked[mod_name] = bad
    assert not leaked, f"forbidden I/O imports leaked: {leaked!r}"


def test_no_phase6_module_imports_runtime_defaults():
    leaked: dict[str, list[str]] = {}
    for mod_name in _PHASE6_MODULES:
        bad = [
            i for i in _module_imports(mod_name)
            if _matches_any(i, _FORBIDDEN_RUNTIME)
        ]
        if bad:
            leaked[mod_name] = bad
    assert not leaked, (
        f"runtime._defaults import leaked into phase-6 module: {leaked!r}"
    )


# ===========================================================================
# Execution-surface pin (carried from phase 4-5)
# ===========================================================================


_FORBIDDEN_CALLABLES = {
    "reason",
    "emit",
    "delegate",
    "transfer",
    "sub_agent",
    "dispatch",
    "invoke",
    "fire",
    "claim",
    "execute",
}


def test_no_phase6_public_callable_suggests_execution_surface():
    """The cache is pure storage + lookup. No public callable
    in any phase-6 module dispatches to reasoning / emit /
    sub-agents / delegate / transfer / fire / claim / execute.
    Pin so a future helper that drifts toward those concepts
    surfaces here for review."""
    import importlib

    leaked: dict[str, list[str]] = {}
    for mod_name in _PHASE6_MODULES:
        mod = importlib.import_module(mod_name)
        for name, value in vars(mod).items():
            if name.startswith("_"):
                continue
            if not callable(value):
                continue
            # Only flag callables DEFINED in this module
            # (not re-exports of pydantic / Protocol).
            module_attr = getattr(value, "__module__", None)
            if module_attr != mod_name:
                continue
            if name.lower() in _FORBIDDEN_CALLABLES:
                leaked.setdefault(mod_name, []).append(name)
    assert not leaked, (
        f"execution-surface callable leaked: {leaked!r}"
    )


# ===========================================================================
# Stale-comment fix in app/v2/models/common.py
# ===========================================================================


def test_channelref_docstring_no_longer_references_phase_3():
    from app.v2.models.common import ChannelRef

    doc = ChannelRef.__doc__ or ""
    assert "phase-3" not in doc
    assert "phase 3" not in doc
    # The new wording is anchored on phase-6 registry cache.
    assert "phase-6 registry cache" in doc


def test_sheetref_docstring_no_longer_references_phase_3():
    from app.v2.models.common import SheetRef

    doc = SheetRef.__doc__ or ""
    assert "phase-3" not in doc
    assert "phase 3" not in doc
    assert "phase-6 registry cache" in doc


def test_channelref_external_id_field_description_updated():
    """The Field(description=...) text used to cite "phase 3";
    pin that the new wording points at phase 6."""
    from app.v2.models.common import ChannelRef

    desc = ChannelRef.model_fields["external_id"].description or ""
    assert "phase 3" not in desc
    assert "phase-3" not in desc
    assert "phase-6 registry cache" in desc


def test_models_common_module_has_no_phase_3_references_anywhere():
    """Belt-and-braces: read the file and grep. The docstring
    of the MODULE may mention the historical renumber, but
    must not contain the literal "phase-3" or "phase 3"
    after slice-5."""
    src = Path("app/v2/models/common.py").read_text(encoding="utf-8")
    assert "phase-3" not in src
    assert "phase 3" not in src
