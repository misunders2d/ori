"""Tests for ``app.v2.runtime._owner_default``.

Phase 9 slice 6 per ``docs/PHASE_9_PLAN.md`` §3.6 + §5.5 +
§6.

Behavioural pins:

- Env populated → :data:`DEFAULT_AUTHORING_OWNER_ID` carries
  the env value (stripped).
- Env missing → :data:`DEFAULT_AUTHORING_OWNER_ID` is
  ``None``.
- Env empty string → treated as missing
  (:data:`DEFAULT_AUTHORING_OWNER_ID` is ``None``).
- Env whitespace-only → treated as missing.
- AuthoringToolset constructor: explicit kwarg wins over
  the env default.
- AuthoringToolset constructor: explicit kwarg ``None`` +
  env default present → env default used.
- AuthoringToolset constructor: explicit kwarg ``None`` +
  env default ``None`` → ``RuntimeError`` with a clear
  startup message.
- AST pin: ``_owner_default.py`` is the SOLE module in
  ``app.v2.runtime`` allowed to read ``os.environ`` at
  module load. Every other ``app.v2.runtime.*`` module is
  AST-walked and asserted to NOT call ``os.environ.get``
  / ``os.getenv`` / subscript ``os.environ[...]`` at
  module level.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import sys

import pytest

import app.v2.runtime as runtime_pkg
from app.v2.runtime import _owner_default
from app.v2.toolsets.authoring import AuthoringToolset


# ===========================================================================
# Module-level env capture
# ===========================================================================


def _reload_owner_default(monkeypatch, value):
    """Re-import ``_owner_default`` with a controlled env so
    the module-level constant captures the desired value.

    ``value`` may be a string (set env to that), or ``None``
    (delete the env var). After re-import the module is
    swapped back into ``sys.modules`` so subsequent imports
    by other tests still get the original captured value
    (set up by ``conftest`` / pytest startup)."""
    if value is None:
        monkeypatch.delenv("V2_AUTHORING_OWNER_ID", raising=False)
    else:
        monkeypatch.setenv("V2_AUTHORING_OWNER_ID", value)
    # Force fresh import so module-level code re-runs.
    sys.modules.pop("app.v2.runtime._owner_default", None)
    return importlib.import_module("app.v2.runtime._owner_default")


def test_env_populated_default_is_env_value(monkeypatch):
    mod = _reload_owner_default(monkeypatch, "T_LIVE")
    try:
        assert mod.DEFAULT_AUTHORING_OWNER_ID == "T_LIVE"
    finally:
        # Restore the original module so other tests pick up
        # the pytest-startup capture.
        sys.modules.pop("app.v2.runtime._owner_default", None)
        importlib.import_module("app.v2.runtime._owner_default")


def test_env_missing_default_is_none(monkeypatch):
    mod = _reload_owner_default(monkeypatch, None)
    try:
        assert mod.DEFAULT_AUTHORING_OWNER_ID is None
    finally:
        sys.modules.pop("app.v2.runtime._owner_default", None)
        importlib.import_module("app.v2.runtime._owner_default")


def test_env_empty_string_treated_as_missing(monkeypatch):
    """A deploy-time mistake (``V2_AUTHORING_OWNER_ID=""``)
    must not register the empty string as a tenant id; it
    must surface the same way as an unset variable so the
    constructor's both-None gate fires."""
    mod = _reload_owner_default(monkeypatch, "")
    try:
        assert mod.DEFAULT_AUTHORING_OWNER_ID is None
    finally:
        sys.modules.pop("app.v2.runtime._owner_default", None)
        importlib.import_module("app.v2.runtime._owner_default")


def test_env_whitespace_only_treated_as_missing(monkeypatch):
    mod = _reload_owner_default(monkeypatch, "   ")
    try:
        assert mod.DEFAULT_AUTHORING_OWNER_ID is None
    finally:
        sys.modules.pop("app.v2.runtime._owner_default", None)
        importlib.import_module("app.v2.runtime._owner_default")


def test_env_value_is_stripped(monkeypatch):
    """Stripped on capture so a leading/trailing space in
    the env value doesn't silently become part of the
    tenant id."""
    mod = _reload_owner_default(monkeypatch, "  T_PADDED  ")
    try:
        assert mod.DEFAULT_AUTHORING_OWNER_ID == "T_PADDED"
    finally:
        sys.modules.pop("app.v2.runtime._owner_default", None)
        importlib.import_module("app.v2.runtime._owner_default")


# ===========================================================================
# AuthoringToolset precedence
# ===========================================================================


def test_explicit_kwarg_wins_over_env_default(monkeypatch):
    """When the explicit kwarg is set, it must win even if
    the env default is populated. Monkeypatch the module
    constant directly (the toolset constructor reads it
    via attribute access on ``_owner_default``)."""
    monkeypatch.setattr(
        _owner_default, "DEFAULT_AUTHORING_OWNER_ID", "T_ENV"
    )
    toolset = AuthoringToolset(expected_owner_id="T_EXPLICIT")
    assert toolset._expected_owner_id == "T_EXPLICIT"


def test_env_fallback_used_when_explicit_is_none(monkeypatch):
    """When the kwarg is omitted (None), the constructor
    must fall back to ``_owner_default.DEFAULT_AUTHORING_OWNER_ID``."""
    monkeypatch.setattr(
        _owner_default, "DEFAULT_AUTHORING_OWNER_ID", "T_ENV"
    )
    toolset = AuthoringToolset()
    assert toolset._expected_owner_id == "T_ENV"


def test_env_fallback_used_when_explicit_is_explicitly_none(monkeypatch):
    """Same as the omitted-kwarg case, but the caller
    explicitly passes ``expected_owner_id=None``. Behaviour
    must match — None is the documented sentinel."""
    monkeypatch.setattr(
        _owner_default, "DEFAULT_AUTHORING_OWNER_ID", "T_ENV"
    )
    toolset = AuthoringToolset(expected_owner_id=None)
    assert toolset._expected_owner_id == "T_ENV"


def test_both_none_raises_runtime_error(monkeypatch):
    """When the kwarg is None AND the env default is None,
    the constructor must raise ``RuntimeError`` so the bot
    refuses to start instead of silently mounting against a
    missing tenant id (phase-9 §3.6 / phase-7 round-2 L365)."""
    monkeypatch.setattr(
        _owner_default, "DEFAULT_AUTHORING_OWNER_ID", None
    )
    with pytest.raises(RuntimeError) as exc_info:
        AuthoringToolset(expected_owner_id=None)
    msg = str(exc_info.value)
    assert "expected_owner_id" in msg
    assert "V2_AUTHORING_OWNER_ID" in msg


def test_both_none_via_omitted_kwarg_raises(monkeypatch):
    """Mirror of the explicit-None case but via omission so
    the precedence story holds for both invocations."""
    monkeypatch.setattr(
        _owner_default, "DEFAULT_AUTHORING_OWNER_ID", None
    )
    with pytest.raises(RuntimeError):
        AuthoringToolset()


# ===========================================================================
# Import-hygiene AST pin — `_owner_default.py` is the SOLE
# `app.v2.runtime.*` module reading os.environ at module
# load (per plan §6 documented exception).
# ===========================================================================


def _module_reads_environ_at_load(mod) -> bool:
    """AST walk: return True iff the module source has a
    module-level expression that reads ``os.environ.get`` /
    ``os.environ[...]`` / ``os.getenv``. Function-body reads
    are ignored (those are runtime, not module-load).
    """
    src = inspect.getsource(mod)
    tree = ast.parse(src)
    # Only inspect top-level statements (Module.body), not
    # nested function bodies.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Skip definitions — runtime reads OK.
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            f = child.func
            # os.environ.get / os.getenv — Attribute chain.
            chain: list[str] = []
            cur = f
            while isinstance(cur, ast.Attribute):
                chain.insert(0, cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                chain.insert(0, cur.id)
            if chain[:2] == ["os", "environ"] and chain[-1] == "get":
                return True
            if chain == ["os", "getenv"]:
                return True
        # Also catch ``os.environ["X"]`` subscript reads.
        for child in ast.walk(node):
            if not isinstance(child, ast.Subscript):
                continue
            v = child.value
            chain2: list[str] = []
            cur = v
            while isinstance(cur, ast.Attribute):
                chain2.insert(0, cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                chain2.insert(0, cur.id)
            if chain2 == ["os", "environ"]:
                return True
    return False


def test_owner_default_module_reads_environ_at_load():
    """Inverse smoke: ``_owner_default.py`` DOES read
    ``os.environ`` at module load (that's its job). A
    regression that gutted the env capture would flip
    here."""
    assert _module_reads_environ_at_load(_owner_default) is True


def test_owner_default_is_sole_runtime_env_reader():
    """Every OTHER ``app.v2.runtime.*`` module MUST NOT read
    ``os.environ`` at module load. ``_owner_default.py`` is
    the documented exception (plan §6). A regression that
    quietly moves env reads into another runtime module
    flips here."""
    leaked: dict[str, bool] = {}
    for module_info in pkgutil.iter_modules(runtime_pkg.__path__):
        name = module_info.name
        if name.startswith("_") and name != "_defaults":
            # ``_owner_default`` IS allowed; every other
            # private module (``_defaults`` etc.) is checked.
            if name == "_owner_default":
                continue
        full = f"app.v2.runtime.{name}"
        mod = importlib.import_module(full)
        if _module_reads_environ_at_load(mod):
            leaked[full] = True
    assert leaked == {}, (
        f"os.environ read at module load leaked into runtime "
        f"modules outside the documented `_owner_default` "
        f"exception: {leaked!r}"
    )


# ===========================================================================
# Public surface pin
# ===========================================================================


def test_owner_default_public_surface_is_constant_only():
    """The module's documented public surface is the single
    constant ``DEFAULT_AUTHORING_OWNER_ID``. Pin so a
    regression that introduces additional public symbols
    flips here (forces a follow-up plan revision)."""
    assert _owner_default.__all__ == ["DEFAULT_AUTHORING_OWNER_ID"]
    assert hasattr(_owner_default, "DEFAULT_AUTHORING_OWNER_ID")
