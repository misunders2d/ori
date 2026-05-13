"""Loader registry tests — registration, lookup, template rendering of
args before invocation, error surfacing on unknown loaders.

Heavy loaders that need external services (BigQuery, Keepa, Neo4j,
Drive, Sheets) are reserved slots with ``NotImplementedError``; we
verify their NAMES are registered and that unknown names raise
``KeyError``. Real integration tests for those loaders land alongside
P7 (where they're wired to the actual SDKs).
"""

from __future__ import annotations

import pytest

from app.contracts.loaders import (
    LOADERS,
    known_loaders,
    run_loader,
)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_known_loaders_contains_full_v1_set():
    """Every loader the AUTHOR pipeline currently knows about must be
    registered. New loaders bump this list — the test reminds the
    author of every Phase 2/Phase 7 loader the system promises."""
    expected = {
        "static_param",
        "web_search",
        "graph_query",
        "memory_search",
        "sheet_read",
        "drive_doc_read",
        "bigquery_query",
        "keepa_get_history",
    }
    assert set(known_loaders()) >= expected


def test_run_loader_raises_keyerror_on_unknown():
    """Resolution by name; unknown name = clear error so the executor
    can route through on_failure rather than silently producing None."""
    with pytest.raises(KeyError, match="unknown loader"):
        import asyncio

        asyncio.run(run_loader("ghost_loader_42", {}, {}))


# ---------------------------------------------------------------------------
# static_param
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_param_returns_value_verbatim():
    out = await run_loader("static_param", {"value": "hello"}, {})
    assert out == "hello"


@pytest.mark.asyncio
async def test_static_param_supports_complex_values():
    """A contract often seeds a list of objects (e.g. five Pilot texts
    with day-of-week keys) via ``static_param``. Dict / list values
    must pass through unchanged."""
    payload = {"mon": "Monday text", "tue": "Tuesday text"}
    out = await run_loader("static_param", {"value": payload}, {})
    assert out == payload


@pytest.mark.asyncio
async def test_static_param_requires_value_arg():
    with pytest.raises(ValueError, match="requires args.value"):
        await run_loader("static_param", {}, {})


@pytest.mark.asyncio
async def test_static_param_renders_template_in_value():
    """Loader args are template-rendered against state BEFORE the
    loader runs. So ``static_param`` with a templated value yields the
    rendered string."""
    out = await run_loader(
        "static_param",
        {"value": "ASIN is {asin}"},
        {"asin": "B0XYZ"},
    )
    assert out == "ASIN is B0XYZ"


# ---------------------------------------------------------------------------
# (The reserved-slot tests were retired once these loaders moved from
# ``NotImplementedError`` stubs to real implementations backed by
# Sheets / Drive / BigQuery / Keepa. Live behaviour is covered by the
# loader-specific integration suites; missing-args paths surface as
# ``ValueError`` and are checked alongside each adapter.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Template rendering of args happens BEFORE the loader sees them
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_args_templated_against_state_before_loader_invoked():
    """run_loader is the integration point with the templating engine.
    Any loader receives already-rendered args; the loader body never
    has to think about {placeholders}.

    Easiest to verify via static_param: we put a {key} in the args and
    confirm the loader sees the resolved value, not the literal brace.
    """
    out = await run_loader(
        "static_param",
        {"value": ["a", "{x}", "c"]},
        {"x": "MIDDLE"},
    )
    assert out == ["a", "MIDDLE", "c"]


# ---------------------------------------------------------------------------
# Register decorator behaviour
# ---------------------------------------------------------------------------


def test_register_decorator_rejects_duplicates():
    """Programming-error guard: re-registering an existing name would
    silently shadow the previous loader. The registry refuses."""
    from app.contracts.loaders import register

    @register("test_unique_only_42")
    async def _fn(args, state):
        return None

    with pytest.raises(ValueError, match="already registered"):

        @register("test_unique_only_42")
        async def _fn2(args, state):
            return None
