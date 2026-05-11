"""Input loaders — deterministic data fetchers, NO LLM.

A loader takes a dict of args (already template-rendered from prior
state) and returns a JSON-serialisable value. Reasoning steps later
reference loader outputs by id (``{news.results[0].url}``).

Loaders intentionally call the underlying SDK / core function, not the
agent-facing tool wrapper. Tool wrappers add LLM-friendly formatting,
state-injection side effects, etc. — neither of which a loader needs.
Going through the core layer keeps fire-time work cheap and predictable.

Templating: ``{state_path}`` placeholders inside string args are
resolved against the fire's ``state`` dict using ``app.contracts.templating.render``.
Lists and nested dicts are walked recursively.

Registry: ``LOADERS`` maps loader name → coroutine ``(args, state) -> value``.
Adding a new loader = registering it here. Schema validation happens at
freeze time (see ``app.contracts.author.draft``); the executor refuses
to fire a contract that references an unknown loader.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from app.contracts.templating import render

logger = logging.getLogger(__name__)


# Loader = (rendered_args, full_state) -> serialisable result
Loader = Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]

LOADERS: dict[str, Loader] = {}


def register(name: str):
    """Decorator-style loader registration."""

    def _wrap(fn: Loader) -> Loader:
        if name in LOADERS:
            raise ValueError(f"loader {name!r} already registered")
        LOADERS[name] = fn
        return fn

    return _wrap


# ---------------------------------------------------------------------------
# static_param — pass a literal through (the trivial case)
# ---------------------------------------------------------------------------


@register("static_param")
async def static_param(args: dict[str, Any], state: dict[str, Any]) -> Any:
    """Return ``args["value"]`` verbatim.

    Used when a contract wants a hard-coded constant available in
    later templates — e.g. the channel name, an ASIN list, the day's
    Pilot text. ``args["value"]`` itself can be a string, dict, list,
    whatever JSON-serialisable thing the author needs.
    """
    if "value" not in args:
        raise ValueError("static_param requires args.value")
    return args["value"]


# ---------------------------------------------------------------------------
# web_search — top-N hits for a query (uses web_fetch tool's core path)
# ---------------------------------------------------------------------------


@register("web_search")
async def web_search(args: dict[str, Any], state: dict[str, Any]) -> list[dict]:
    """Search the web and return up to ``limit`` results.

    Currently delegates to ``app.tools.web.web_fetch`` for a single
    URL fetch; full-text web search (DuckDuckGo / SerpAPI / etc.) is
    a follow-up. For now ``args.url`` is required and we return a
    one-element list of ``{url, title, snippet}`` so contracts that
    expect a list shape don't break.
    """
    from app.tools.web import web_fetch
    from app.contracts._loader_context import LoaderContext

    if "url" not in args:
        raise ValueError(
            "web_search currently requires args.url (full-text search "
            "TBD). For now use static_param to seed URLs or chain "
            "from another loader."
        )

    res = web_fetch(args["url"], LoaderContext())
    if isinstance(res, dict) and res.get("status") == "success":
        return [
            {
                "url": args["url"],
                "title": res.get("title", ""),
                "snippet": (res.get("text") or "")[:500],
                "fetched_at": res.get("fetched_at"),
            }
        ]
    return []


# ---------------------------------------------------------------------------
# graph_query — Cypher against Neo4j (direct driver call)
# ---------------------------------------------------------------------------


@register("graph_query")
async def graph_query(args: dict[str, Any], state: dict[str, Any]) -> list[dict]:
    """Run a parameterised Cypher query against the project Neo4j
    instance and return all rows as dicts.

    ``args.cypher`` is the query; ``args.params`` (optional) is a dict
    of bind parameters. The driver is the singleton from
    ``app.core.graph``; we do NOT open a fresh connection per fire.
    """
    from app.core.graph import _get_driver, is_configured

    if not is_configured():
        raise RuntimeError(
            "graph_query loader: Neo4j is not configured. "
            "Check NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD in the vault."
        )
    if "cypher" not in args:
        raise ValueError("graph_query requires args.cypher")

    cypher: str = args["cypher"]
    params: dict[str, Any] = args.get("params") or {}

    driver = _get_driver()
    async with driver.session() as session:
        result = await session.run(cypher, **params)
        rows = [dict(r) async for r in result]
    return rows


# ---------------------------------------------------------------------------
# memory_search — semantic recall against the knowledge graph
# ---------------------------------------------------------------------------


@register("memory_search")
async def memory_search(args: dict[str, Any], state: dict[str, Any]) -> list[dict]:
    """Semantic search across Memory nodes. Returns ``args.limit`` results
    (default 10). ``args.query`` is the search text; ``args.namespace``
    can narrow to ``personal | professional | technical``.

    Delegates to the underlying ``search_knowledge`` helper with a
    synthetic ``LoaderContext`` so it can run outside an agent session.
    """
    from app.tools.memory_tools import search_knowledge
    from app.contracts._loader_context import LoaderContext

    if "query" not in args:
        raise ValueError("memory_search requires args.query")

    res = await search_knowledge(
        query=args["query"],
        namespace=args.get("namespace") or "professional",
        limit=int(args.get("limit", 10)),
        tool_context=LoaderContext(),
    )
    if isinstance(res, dict):
        return res.get("results", [])
    return []


# ---------------------------------------------------------------------------
# sheet_read — read rows from a Google Sheet
# ---------------------------------------------------------------------------


@register("sheet_read")
async def sheet_read(args: dict[str, Any], state: dict[str, Any]) -> list[list]:
    """Read a range from a Google Sheet. ``args.spreadsheet_id`` +
    ``args.range`` (A1 notation, e.g. ``"Sheet1!A1:C100"``). Returns a
    list of rows (each row a list of cell values).

    Authenticates via the project's stored Google OAuth tokens (same
    path as the agent-facing google_drive tools).
    """
    # Wire into the existing sheets path. For now we surface a clear
    # NotImplementedError so contracts that need this loader fail
    # loudly during dry-run rather than silently returning empty.
    raise NotImplementedError(
        "sheet_read loader is wired in P7 (the AI Pilot migration uses "
        "it). For P2 the registry slot is reserved; tests assert that "
        "the loader name is known."
    )


# ---------------------------------------------------------------------------
# drive_doc_read — read a Google Doc as plain text
# ---------------------------------------------------------------------------


@register("drive_doc_read")
async def drive_doc_read(args: dict[str, Any], state: dict[str, Any]) -> str:
    """Download a Google Doc as plain text. ``args.doc_id`` required."""
    raise NotImplementedError(
        "drive_doc_read loader is wired in P7 (the 30-step ASIN audit "
        "uses it). Registry slot reserved for P2."
    )


# ---------------------------------------------------------------------------
# bigquery_query — run BigQuery SQL, return rows
# ---------------------------------------------------------------------------


@register("bigquery_query")
async def bigquery_query(args: dict[str, Any], state: dict[str, Any]) -> list[dict]:
    """Run BigQuery SQL and return rows as dicts.

    Uses the project's configured BigQuery client (same credentials path
    as ``BigQueryAgent``). ``args.sql`` is the query; optional
    ``args.params`` for parameterised queries.
    """
    raise NotImplementedError(
        "bigquery_query loader is wired in P7. Registry slot reserved."
    )


# ---------------------------------------------------------------------------
# keepa_get_history — Keepa price/BSR history for an ASIN
# ---------------------------------------------------------------------------


@register("keepa_get_history")
async def keepa_get_history(args: dict[str, Any], state: dict[str, Any]) -> dict:
    """Pull Keepa price + BSR history for ``args.asin`` over the past
    ``args.days`` (default 90). Returns a summary dict."""
    raise NotImplementedError(
        "keepa_get_history loader is wired in P7. Registry slot reserved."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_loader(name: str, args: dict[str, Any], state: dict[str, Any]) -> Any:
    """Resolve template placeholders in ``args`` against ``state``, then
    invoke the named loader.

    Raises ``KeyError`` if the loader name isn't registered — the
    AUTHOR pipeline validates loader names at draft time, so this
    should only fire when a frozen contract was authored against a
    newer schema than the current code.
    """
    if name not in LOADERS:
        raise KeyError(
            f"unknown loader {name!r}. Known: {sorted(LOADERS.keys())}"
        )
    rendered = render(args, state)
    return await LOADERS[name](rendered, state)


def known_loaders() -> list[str]:
    """List of registered loader names. Used by the AUTHOR pipeline to
    validate references at draft time."""
    return sorted(LOADERS.keys())
