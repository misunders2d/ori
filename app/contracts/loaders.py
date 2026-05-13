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

# Per-loader argument contract. Populated by ``register`` when given
# schema kwargs. Consumed by
# ``app.contracts.validation.validate_adapter_arg_shapes`` at author
# time. Required-arg check accepts either the canonical key or any of
# its aliases; optional keys are allowed but never required; unknown
# keys are rejected so typos get caught at freeze.
LOADER_ARG_SCHEMAS: dict[str, dict[str, Any]] = {}


def register(
    name: str,
    *,
    required: list[str] | None = None,
    optional: list[str] | None = None,
    aliases: dict[str, list[str]] | None = None,
):
    """Decorator-style loader registration.

    Schema kwargs (all optional):
      - ``required``: arg keys that MUST appear in ``inputs[].args``
        (or one of their aliases).
      - ``optional``: arg keys that MAY appear. Listed so the validator
        rejects unknown keys (typos).
      - ``aliases``: ``{canonical_key: [alias_a, alias_b]}``.

    Loaders without schema kwargs declare no contract — author-time
    validator skips them. New loaders should always declare.
    """

    def _wrap(fn: Loader) -> Loader:
        if name in LOADERS:
            raise ValueError(f"loader {name!r} already registered")
        LOADERS[name] = fn
        if required or optional or aliases:
            LOADER_ARG_SCHEMAS[name] = {
                "required": list(required or []),
                "optional": list(optional or []),
                "aliases": {k: list(v) for k, v in (aliases or {}).items()},
            }
        return fn

    return _wrap


# ---------------------------------------------------------------------------
# static_param — pass a literal through (the trivial case)
# ---------------------------------------------------------------------------


@register("static_param", required=["value"])
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


@register("web_search", required=["url"])
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


@register("graph_query", required=["cypher"], optional=["params"])
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


@register("memory_search", required=["query"], optional=["namespace", "limit"])
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


def _contract_author(state: dict[str, Any]) -> str:
    meta = state.get("__contract__") or {}
    author = meta.get("author") or ""
    if not author:
        raise RuntimeError(
            "contract author missing from state.__contract__; per-user "
            "OAuth loaders cannot resolve credentials."
        )
    return author


@register(
    "sheet_read",
    required=["spreadsheet_id"],
    optional=["range"],
    aliases={"spreadsheet_id": ["source"]},
)
async def sheet_read(args: dict[str, Any], state: dict[str, Any]) -> list[list]:
    """Read a range from a Google Sheet. ``args.spreadsheet_id`` (or
    full URL) + ``args.range`` (A1 notation, e.g. ``"Sheet1!A1:C100"``;
    defaults to ``"Sheet1"`` if omitted). Returns a list of rows (each
    row a list of cell values).

    Auth: contract author's stored Google OAuth.
    """
    import httpx

    spreadsheet_id_raw = args.get("spreadsheet_id") or args.get("source") or ""
    if not spreadsheet_id_raw:
        raise ValueError("sheet_read requires args.spreadsheet_id")

    from app.tools.google_drive import _extract_drive_id, _get_valid_token
    spreadsheet_id = _extract_drive_id(spreadsheet_id_raw)
    rng = args.get("range") or "Sheet1"

    author = _contract_author(state)
    token = await _get_valid_token(author)
    if not token:
        raise RuntimeError(
            f"Google not connected for author {author!r}. Author must "
            f"run `google_connect` from chat to authorize."
        )

    api = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{rng}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(api, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Sheets {resp.status_code} on values:get "
                f"spreadsheet_id={spreadsheet_id!r} range={rng!r}: "
                f"{resp.text[:300]}"
            )
        data = resp.json()
    return data.get("values", []) or []


# ---------------------------------------------------------------------------
# drive_doc_read — read a Google Doc as plain text
# ---------------------------------------------------------------------------


@register(
    "drive_doc_read",
    required=["doc_id"],
    aliases={"doc_id": ["document_id"]},
)
async def drive_doc_read(args: dict[str, Any], state: dict[str, Any]) -> str:
    """Download a Google Doc as plain text. ``args.doc_id`` accepts a
    Doc ID OR full URL.

    Auth: contract author's stored Google OAuth.
    """
    import httpx

    doc_id_raw = args.get("doc_id") or args.get("document_id") or ""
    if not doc_id_raw:
        raise ValueError("drive_doc_read requires args.doc_id")

    from app.tools.google_drive import _extract_drive_id, _get_valid_token
    doc_id = _extract_drive_id(doc_id_raw)

    author = _contract_author(state)
    token = await _get_valid_token(author)
    if not token:
        raise RuntimeError(
            f"Google not connected for author {author!r}. Author must "
            f"run `google_connect` from chat to authorize."
        )

    # Use Drive `files.export` to text/plain — uniform path for Docs/Sheets/Slides.
    api = f"https://www.googleapis.com/drive/v3/files/{doc_id}/export"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            api,
            params={"mimeType": "text/plain"},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Drive {resp.status_code} on files.export "
                f"doc_id={doc_id!r}: {resp.text[:300]}"
            )
        text = resp.text
    return text


# ---------------------------------------------------------------------------
# bigquery_query — run BigQuery SQL, return rows
# ---------------------------------------------------------------------------


@register(
    "bigquery_query",
    required=["sql"],
    optional=["params", "project_id"],
)
async def bigquery_query(args: dict[str, Any], state: dict[str, Any]) -> list[dict]:
    """Run BigQuery SQL and return rows as dicts.

    Args:
      - ``sql`` (str, required): the query
      - ``params`` (dict, optional): named query parameters
      - ``project_id`` (str, optional): GCP project; defaults to the
        env-configured project

    Auth: service account from ``BQ_GCP_SERVICE_ACCOUNT_INFO`` env
    (same credentials path the BigQueryAgent uses). No per-user OAuth.
    """
    import asyncio
    import json
    import os

    sql = args.get("sql")
    if not sql:
        raise ValueError("bigquery_query requires args.sql")

    try:
        from google.cloud import bigquery
        from google.oauth2 import service_account
    except ImportError as e:
        raise RuntimeError(f"google-cloud-bigquery not installed: {e}")

    sa_info_raw = os.environ.get("BQ_GCP_SERVICE_ACCOUNT_INFO", "")
    if not sa_info_raw:
        raise RuntimeError(
            "BQ_GCP_SERVICE_ACCOUNT_INFO env var not set — BigQuery loader "
            "needs the same service-account credentials as BigQueryAgent."
        )
    try:
        sa_info = json.loads(sa_info_raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"BQ_GCP_SERVICE_ACCOUNT_INFO is not valid JSON: {e}")

    project_id = args.get("project_id") or sa_info.get("project_id") or ""
    credentials = service_account.Credentials.from_service_account_info(sa_info)

    def _run_query() -> list[dict]:
        client = bigquery.Client(credentials=credentials, project=project_id)
        params = args.get("params") or {}
        job_config = None
        if params:
            query_params = [
                bigquery.ScalarQueryParameter(name, "STRING", str(value))
                for name, value in params.items()
            ]
            job_config = bigquery.QueryJobConfig(query_parameters=query_params)
        result = client.query(sql, job_config=job_config).result()
        return [dict(row.items()) for row in result]

    return await asyncio.to_thread(_run_query)


# ---------------------------------------------------------------------------
# keepa_get_history — Keepa price/BSR history for an ASIN
# ---------------------------------------------------------------------------


@register("keepa_get_history", required=["asin"], optional=["domain"])
async def keepa_get_history(args: dict[str, Any], state: dict[str, Any]) -> dict:
    """Pull Keepa price + BSR history for ``args.asin``.

    Args:
      - ``asin`` (str, required)
      - ``domain`` (int, optional): Keepa domain id (default 1 = .com)

    Auth: ``KEEPA_API_KEY`` env (no per-user auth). Result includes the
    cached lightweight summary from the existing keepa_fetch_product
    tool — extraction of specific history series is the caller's job
    via additional reasoning steps.
    """
    from app.tools.keepa_api import keepa_fetch_product
    from app.contracts._loader_context import LoaderContext

    asin = args.get("asin")
    if not asin:
        raise ValueError("keepa_get_history requires args.asin")
    domain = int(args.get("domain", 1))

    result = await keepa_fetch_product(
        asin=asin,
        domain=domain,
        tool_context=LoaderContext(),
    )
    if isinstance(result, dict) and result.get("status") == "error":
        raise RuntimeError(
            f"keepa_get_history failed for asin={asin!r}: "
            f"{result.get('message', '')[:300]}"
        )
    return result


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
