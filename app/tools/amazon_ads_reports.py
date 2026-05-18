"""Deterministic Amazon Ads reporting helper layer.

Goal: the LLM should rarely hand-write the deeply nested Amazon Ads
reporting payload. This module turns a small set of natural parameters
(period / date range / timezone / account / report family / marketplace)
into a correct payload, runs create → poll → retrieve, fetches + parses
the result, and returns a compact masked summary.

It is intentionally BROAD, not a single hardcoded "yesterday" path:

* periods — ``yesterday`` (default), ``today``, ``last_7_days``,
  ``last_30_days``, ``this_month``, ``last_month``, or an explicit
  ``start_date`` / ``end_date`` (``period="custom"``).
* timezone — naked day references resolve in this tz (default
  ``America/Los_Angeles``; the user is Pacific).
* account — resolved by case-insensitive name substring, or an explicit
  ``advertiser_account_id`` override.
* report family — ``campaign`` / ``targeting`` (query-schema, supports a
  ``marketplace`` country filter) or ``product`` / ``inventory``
  (no-query schema). ``custom`` accepts an explicit ``fields`` list.

For asks this layer does not model, the agent still has the raw
``reporting-*`` MCP tools plus the ``ads-reporting`` skill recipes
(`skills/ads-reporting/references/payload-recipes.md`) to derive a
payload by hand.

Exposed as a ``FunctionTool`` on ``AmazonAdsToolset`` (same Amazon-Ads
area — ``docs/AI_EDITS.md`` §2). Rule 13: every failure returns
``{"status": "error", "message": ...}``; no secrets (tokens / headers /
account-ids / presigned URLs) are returned or logged.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TZ = "America/Los_Angeles"

# Field presets — verified live 2026-05-17 unless noted.
_CAMPAIGN_FIELDS = [
    "budgetCurrency.value",
    "dateRange.value",
    "campaign.id",
    "campaign.name",
    "metric.impressions",
    "metric.clicks",
    "metric.totalCost",
    "metric.sales",
    "metric.purchases",
    "metric.unitsSold",
]
# F5: targeting is REJECTED until its field names are byte-verified
# (`target.id` returned 400005 during bring-up). Not exposed as a family;
# the helper errors loudly if asked for it. Re-enable only with a verified
# field preset.
_REJECTED_FAMILIES = {"targeting"}
_QUERY_FAMILIES = {
    "campaign": _CAMPAIGN_FIELDS,
}
# product / inventory use the no-query schema (reports[] requires
# format+periods, `query` is rejected — verified 2026-05-17).
_NOQUERY_FAMILIES = {"product", "inventory"}

# metric.* column -> short label for the summed totals block.
_SUM_METRICS = {
    "metric.impressions": "impressions",
    "metric.clicks": "clicks",
    "metric.totalCost": "cost",
    "metric.sales": "sales",
    "metric.purchases": "orders",
    "metric.unitsSold": "units",
}

_POLL_INTERVAL = 15.0
# Production-sensible, NOT infinite. Reports often take a few minutes.
# Tunable via vault ADS_API_REPORT_POLL_SECONDS; clamped to a hard ceiling
# so a stuck report can never block an agent turn forever.
_DEFAULT_POLL_SECONDS = 600
_MIN_POLL_SECONDS = 60
_MAX_POLL_SECONDS = 1200


def _resolve_poll_budget() -> int:
    """Total seconds to poll, from vault (default 600), clamped
    [60, 1200]. Invalid → default."""
    from app.tools import amazon_ads_auth as auth

    raw = auth.vault.get("ADS_API_REPORT_POLL_SECONDS", "").strip()
    try:
        secs = int(raw) if raw else _DEFAULT_POLL_SECONDS
    except ValueError:
        secs = _DEFAULT_POLL_SECONDS
    return max(_MIN_POLL_SECONDS, min(_MAX_POLL_SECONDS, secs))


def _poll_tries() -> int:
    return max(1, int(_resolve_poll_budget() / _POLL_INTERVAL))


def _pending_result(
    *,
    account_label: str,
    marketplace: str,
    report_family: str,
    period: str,
    start: str,
    end: str,
    waited_s: int,
) -> dict:
    """Sanitized 'still generating' result — distinct status so the bot
    relays a retry hint. Carries ONLY deterministic request params (no
    reportId, no URL, no account id) so the user can simply re-issue the
    same ask to resume/retry."""
    return {
        "status": "pending",
        "message": (
            f"Amazon is still generating the {report_family} report for "
            f"{start}..{end} (waited {waited_s}s). It was accepted and is "
            "queued server-side. Re-issue the same request shortly to "
            "fetch it — the request is deterministic, no data was lost."
        ),
        "retry": {
            "account": account_label,
            "marketplace": marketplace or None,
            "report_family": report_family,
            "period": period,
            "date_range": {"start": start, "end": end},
        },
    }


def _resolve_period(
    period: str, start_date: str, end_date: str, tz: str
) -> tuple[str, str] | str:
    """Return ``(start, end)`` ISO dates, or an error string."""
    try:
        zone = ZoneInfo(tz or _DEFAULT_TZ)
    except Exception:
        return f"Unknown timezone {tz!r}."
    today = datetime.now(zone).date()
    p = (period or "yesterday").strip().lower()
    if p == "custom":
        if not start_date or not end_date:
            return "period='custom' requires start_date and end_date."
        try:
            date.fromisoformat(start_date)
            date.fromisoformat(end_date)
        except ValueError:
            return "start_date / end_date must be YYYY-MM-DD."
        return start_date, end_date
    if p == "today":
        return today.isoformat(), today.isoformat()
    if p == "yesterday":
        d = today - timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if p in ("last_7_days", "last7", "7d"):
        return (today - timedelta(days=7)).isoformat(), (
            today - timedelta(days=1)
        ).isoformat()
    if p in ("last_30_days", "last30", "30d"):
        return (today - timedelta(days=30)).isoformat(), (
            today - timedelta(days=1)
        ).isoformat()
    if p in ("this_month", "mtd"):
        return today.replace(day=1).isoformat(), today.isoformat()
    if p in ("last_month", "previous_month"):
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return last_prev.replace(day=1).isoformat(), last_prev.isoformat()
    return (
        f"Unknown period {period!r}. Use yesterday|today|last_7_days|"
        "last_30_days|this_month|last_month|custom."
    )


class _UnrecognizedShape(RuntimeError):
    """A response did not match a pinned, documented path.

    F3/F4: we do NOT deep-recurse arbitrary JSON guessing at id/status/url
    key names. If the response is not in a shape we have pinned, fail loud
    (the message carries only top-level key NAMES — non-sensitive — to aid
    future pinning) rather than silently mis-reading it.
    """


class _ApiError(RuntimeError):
    """Amazon returned a structured error inside the MCP envelope.

    Distinct from ``_UnrecognizedShape`` (we don't know the shape): here
    the shape IS known and Amazon explicitly reported a failure. The
    helper relays this verbatim to the user (ids masked).
    """


def _top_keys(obj: Any) -> list[str]:
    return sorted(obj.keys()) if isinstance(obj, dict) else [type(obj).__name__]


def _mask_ids(s: str) -> str:
    """Strip account/id-looking tokens from text surfaced to the user."""
    s = re.sub(r"amzn1[\w.\-]+", "<id>", s)
    s = re.sub(r"\bg\.[a-z0-9]{12,}\b", "<id>", s)
    s = re.sub(r"\b\d{10,}\b", "<num>", s)
    return s[:400]


def _unwrap_envelope(obj: Any, what: str) -> Any:
    """Amazon's MCP wraps tool results as ``{"error":<x>,"success":<y>}``
    (verified live 2026-05-18). Some tools (query_advertiser_account) are
    NOT enveloped — those pass through unchanged.

    * ``error`` truthy  → ``_ApiError`` (relay verbatim, ids masked).
    * ``error`` falsy   → return ``success`` (the real payload).
    * neither usable    → ``_UnrecognizedShape``.
    """
    if not isinstance(obj, dict) or not (
        "success" in obj and "error" in obj
    ):
        return obj  # not the envelope (or already unwrapped)
    err = obj.get("error")
    if err:
        raise _ApiError(f"Amazon {what} error: {_mask_ids(str(err))}")
    succ = obj.get("success")
    if succ is None:
        raise _UnrecognizedShape(
            f"{what}: envelope has neither error nor success "
            f"(keys: {_top_keys(obj)})"
        )
    return succ


def _report_objs(payload: Any, what: str) -> list[dict]:
    """Normalize the create/retrieve `success` payload to a list of
    ``report`` dicts. Pinned shape (verified): ``[{"index":int,
    "report":{...}}, ...]``; also tolerates a bare report dict / list."""
    items = payload if isinstance(payload, list) else [payload]
    out: list[dict] = []
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("report"), dict):
            out.append(it["report"])
        elif isinstance(it, dict) and "reportId" in it:
            out.append(it)  # already a bare report dict
    if not out:
        raise _UnrecognizedShape(
            f"{what}: success payload had no report objects "
            f"(type={type(payload).__name__})"
        )
    return out


def _extract_accounts(qobj: Any) -> list[tuple[str, str]]:
    """Pinned (verified live 2026-05-18). query_advertiser_account is NOT
    enveloped, but unwrap defensively in case that changes. Shape:
    ``{"advertiserAccounts":[{"advertiserAccountId","displayName",...}]}``.
    Name == ``displayName``. Fail loud otherwise.
    """
    qobj = _unwrap_envelope(qobj, "query_advertiser_account")
    if not isinstance(qobj, dict) or "advertiserAccounts" not in qobj:
        raise _UnrecognizedShape(
            "query_advertiser_account: missing 'advertiserAccounts' "
            f"(top-level keys: {_top_keys(qobj)})"
        )
    accts = qobj["advertiserAccounts"]
    if not isinstance(accts, list):
        raise _UnrecognizedShape(
            "query_advertiser_account: 'advertiserAccounts' is not a list"
        )
    out: list[tuple[str, str]] = []
    for a in accts:
        if not isinstance(a, dict):
            continue
        name = a.get("displayName")
        aid = a.get("advertiserAccountId")
        if isinstance(name, str) and isinstance(aid, str):
            out.append((name, aid))
    return out


def _extract_report_ids(cobj: Any) -> list[str]:
    """Envelope-aware. Pinned: ``success[].report.reportId``. Any
    Amazon-side error in the envelope is raised verbatim (``_ApiError``);
    an unknown shape fails loud (``_UnrecognizedShape``)."""
    payload = _unwrap_envelope(cobj, "create_report")
    ids: list[str] = []
    for rep in _report_objs(payload, "create_report"):
        v = rep.get("reportId") or rep.get("id")
        if isinstance(v, str):
            ids.append(v)
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise _UnrecognizedShape(
            "create_report: no reportId in success[].report "
            f"(top-level keys: {_top_keys(cobj)})"
        )
    return ids


def _extract_status(robj: Any) -> tuple[str, str | None]:
    """Envelope-aware. Returns ``(STATUS, failure_detail_or_None)`` from
    ``success[].report.{status,failureReason,failureCode}``. Fail loud if
    no status."""
    payload = _unwrap_envelope(robj, "retrieve_report")
    for rep in _report_objs(payload, "retrieve_report"):
        st = rep.get("status") or rep.get("state")
        if isinstance(st, str):
            detail = rep.get("failureReason") or rep.get("failureCode")
            return st.upper(), (
                _mask_ids(str(detail)) if detail else None
            )
    raise _UnrecognizedShape(
        "retrieve_report: no status in success[].report "
        f"(top-level keys: {_top_keys(robj)})"
    )


def _extract_result_url(robj: Any) -> str | None:
    """Envelope-aware. Pinned container: ``success[].report`` and its
    ``completedReportParts`` list. Returns the first https URL found in
    those (and only those) locations."""
    try:
        payload = _unwrap_envelope(robj, "retrieve_report")
        reps = _report_objs(payload, "retrieve_report")
    except (_ApiError, _UnrecognizedShape):
        return None
    url_keys = ("url", "downloadUrl", "location", "reportPartUrl", "uri")
    for rep in reps:
        for key in url_keys:
            v = rep.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
        parts = rep.get("completedReportParts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                for key in url_keys:
                    v = part.get(key)
                    if isinstance(v, str) and v.startswith("http"):
                        return v
                # last resort, still bounded to the part dict
                for v in part.values():
                    if isinstance(v, str) and v.startswith("http"):
                        return v
    return None


def _parse_report_csv(
    raw: bytes,
) -> tuple[int, dict[str, float], list[str], bool]:
    """Sum the metric columns we explicitly requested (the documented
    contract — column == the `metric.*` field we asked for). If NONE of
    them are present, return ``metrics_resolved=False`` so the caller
    surfaces it loudly instead of reporting silent zeros."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = list(reader.fieldnames or [])
    totals: dict[str, float] = {}
    rows = 0
    matched = any(f in cols for f in _SUM_METRICS)
    for row in reader:
        rows += 1
        for field, label in _SUM_METRICS.items():
            val = row.get(field)
            if val in (None, ""):
                continue
            try:
                totals[label] = round(totals.get(label, 0.0) + float(val), 4)
            except (TypeError, ValueError):
                continue
    return rows, totals, cols, matched


def _build_body(
    family: str,
    acct_id: str,
    start: str,
    end: str,
    marketplace: str,
    fields: list[str] | None,
) -> dict | str:
    report: dict[str, Any] = {
        "format": "CSV",
        "currencyOfView": "USD",
        "periods": [{"datePeriod": {"startDate": start, "endDate": end}}],
    }
    if family in _QUERY_FAMILIES or family == "custom":
        qfields = fields if (family == "custom" and fields) else _QUERY_FAMILIES.get(
            family, _CAMPAIGN_FIELDS
        )
        report["query"] = {"fields": qfields}
        if marketplace:
            report["query"]["filter"] = {
                "on": {
                    "comparisonOperator": "EQUALS",
                    "field": "campaign.country",
                    "not": False,
                    "values": [marketplace],
                }
            }
    elif family in _NOQUERY_FAMILIES:
        pass  # no-query schema: account + format + periods only
    else:
        return (
            f"Unknown report family {family!r}. Use campaign|targeting|"
            "product|inventory|custom."
        )
    return {
        "accessRequestedAccounts": [{"advertiserAccountId": acct_id}],
        "reports": [report],
    }


def _tool_for_family(family: str) -> str:
    return {
        "campaign": "reporting-create_report",
        "targeting": "reporting-create_report",
        "custom": "reporting-create_report",
        "product": "reporting-create_product_report",
        "inventory": "reporting-create_inventory_report",
    }[family]


class _HelperToolGuard(RuntimeError):
    """Helper tried to call a tool outside its read-only contract."""


# This helper is a raw MCP path — it does NOT pass through
# McpToolset(tool_filter=) or the before_tool allowlist guardrail. So it
# enforces the gate itself, fail-closed: every tool it invokes MUST be on
# the approved allowlist AND read-only. State-changing tools are denied
# here even though they are on the 23-tool allowlist (the helper has no
# business mutating). A future edit that points a family at a mutating
# tool fails loudly instead of silently bypassing the gate.
_HELPER_DENY_STATE_CHANGING = frozenset(
    {
        "account_management-update_account_timezone",
        "campaign_management-dsp_create_conversion_tracking_products",
        "reporting-delete_report",
    }
)


async def _safe_call(session: Any, name: str, args: dict, timeout: float):
    import asyncio

    from app.toolsets.amazon_ads import AMAZON_ADS_ALLOWED_TOOLS

    if name not in AMAZON_ADS_ALLOWED_TOOLS:
        raise _HelperToolGuard(
            f"blocked: {name!r} is not on the approved Amazon Ads allowlist"
        )
    if name in _HELPER_DENY_STATE_CHANGING:
        raise _HelperToolGuard(
            f"blocked: {name!r} is state-changing; the reporting helper is "
            f"read-only by contract"
        )
    return await asyncio.wait_for(
        session.call_tool(name, args), timeout=timeout
    )


async def amazon_ads_performance_report(
    account: str = "",
    marketplace: str = "US",
    period: str = "yesterday",
    report: str = "campaign",
    start_date: str = "",
    end_date: str = "",
    timezone: str = _DEFAULT_TZ,
    advertiser_account_id: str = "",
    fields: list[str] | None = None,
    tool_context: Any = None,
) -> dict:
    """Run an Amazon Ads report deterministically and return a summary.

    Use this for most Ads performance asks instead of hand-writing
    ``reporting-*`` payloads.

    Args:
      account: case-insensitive substring matched against the advertiser
        account name. **Required** unless ``advertiser_account_id`` is
        given — there is no default account.
      marketplace: ISO country code; applied as a ``campaign.country``
        filter for query families (``campaign``/``custom``). Ignored for
        ``product``/``inventory`` (no-query schema).
      period: ``yesterday`` (default) | ``today`` | ``last_7_days`` |
        ``last_30_days`` | ``this_month`` | ``last_month`` | ``custom``.
      report: ``campaign`` (default) | ``product`` | ``inventory`` |
        ``custom``. (``targeting`` is disabled — fields unverified.)
      start_date / end_date: ``YYYY-MM-DD``, required when
        ``period='custom'``.
      timezone: IANA tz for relative periods (default America/Los_Angeles).
      advertiser_account_id: explicit account id override (skips name
        lookup).
      fields: explicit metric/dimension field list — only for
        ``report='custom'``.

    Returns ``{"status": "success", ...}`` with totals + metadata;
    ``{"status": "pending", "retry": {...}}`` if Amazon is still
    generating at the poll budget (re-issue the same request to resume —
    no ids/URLs exposed); or ``{"status": "error", "message": ...}``
    (Rule 13).
    """
    from app.tools import amazon_ads_auth as auth

    if not auth.credentials_present():
        return {
            "status": "error",
            "message": (
                "Amazon Ads not configured — LWA credentials absent from "
                "the vault. Run `uv run python scripts/ads_oauth_helper.py`."
            ),
        }

    fam = (report or "campaign").strip().lower()
    if fam in _REJECTED_FAMILIES:
        return {
            "status": "error",
            "message": (
                f"Report family {report!r} is disabled — its field names "
                "are not verified against the live API. Use campaign|"
                "product|inventory|custom."
            ),
        }
    if fam not in (set(_QUERY_FAMILIES) | _NOQUERY_FAMILIES | {"custom"}):
        return {
            "status": "error",
            "message": (
                f"Unknown report family {report!r}. Use campaign|product|"
                "inventory|custom."
            ),
        }

    if not account.strip() and not advertiser_account_id.strip():
        return {
            "status": "error",
            "message": (
                "An account is required: pass `account` (name substring) "
                "or `advertiser_account_id`. There is no default account."
            ),
        }
    mkt = (marketplace or "").strip().upper()
    rng = _resolve_period(period, start_date, end_date, timezone)
    if isinstance(rng, str):
        return {"status": "error", "message": rng}
    start, end = rng

    try:
        import asyncio
        import json

        await asyncio.to_thread(auth._token_cache.ensure_warm)
        headers = auth.header_provider(None)
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(auth.mcp_url(), headers=headers) as (
            r,
            w,
            _,
        ):
            async with ClientSession(r, w) as s:
                await asyncio.wait_for(s.initialize(), timeout=30)

                # Resolve account. The id never leaves this function and
                # account NAMES are never returned (F2 — no real-account
                # leak); a miss reports only a count.
                if advertiser_account_id.strip():
                    acct_id = advertiser_account_id.strip()
                    acct_label = "(explicit id)"
                else:
                    qa = await _safe_call(
                        s,
                        "account_management-query_advertiser_account",
                        {"body": {}},
                        45,
                    )
                    qtext = "".join(
                        getattr(c, "text", "") for c in (qa.content or [])
                    )
                    try:
                        qobj = json.loads(qtext)
                    except Exception:
                        return {
                            "status": "error",
                            "message": (
                                "query_advertiser_account returned "
                                "unparseable JSON."
                            ),
                        }
                    try:
                        pairs = _extract_accounts(qobj)
                    except _ApiError as exc:
                        return {
                            "status": "error",
                            "message": (
                                f"Amazon errored listing advertiser "
                                f"accounts: {exc}"
                            ),
                        }
                    except _UnrecognizedShape as exc:
                        return {
                            "status": "error",
                            "message": (
                                f"Unrecognized query_advertiser_account "
                                f"shape — {exc}"
                            ),
                        }
                    hint = account.strip().lower()
                    matches = [(n, a) for n, a in pairs if hint in n.lower()]
                    if not matches:
                        return {
                            "status": "error",
                            "message": (
                                f"No advertiser account matched the given "
                                f"name filter ({len(pairs)} account(s) "
                                "accessible). Refine the account name or "
                                "pass advertiser_account_id."
                            ),
                        }
                    if len(matches) > 1:
                        return {
                            "status": "error",
                            "message": (
                                f"Account name filter is ambiguous — "
                                f"{len(matches)} accounts match. Be more "
                                "specific or pass advertiser_account_id."
                            ),
                        }
                    acct_label, acct_id = "(resolved by name)", matches[0][1]

                body = _build_body(fam, acct_id, start, end, mkt, fields)
                if isinstance(body, str):
                    return {"status": "error", "message": body}

                tool = _tool_for_family(fam)
                cr = await _safe_call(s, tool, {"body": body}, 60)
                ctext = "".join(
                    getattr(c, "text", "") for c in (cr.content or [])
                )
                if getattr(cr, "isError", False):
                    return {
                        "status": "error",
                        "message": (
                            f"{tool} rejected ({fam}/{mkt or '-'}/"
                            f"{start}..{end}): {ctext[:240]}"
                        ),
                    }
                try:
                    cobj = json.loads(ctext)
                except Exception:
                    return {
                        "status": "error",
                        "message": f"{tool} returned unparseable JSON.",
                    }
                try:
                    ids = _extract_report_ids(cobj)
                except _ApiError as exc:
                    return {
                        "status": "error",
                        "message": (
                            f"Amazon rejected the {fam} report request "
                            f"({mkt or '-'}/{start}..{end}): {exc}"
                        ),
                    }
                except _UnrecognizedShape as exc:
                    return {
                        "status": "error",
                        "message": (
                            f"Unrecognized {tool} response shape — {exc}"
                        ),
                    }

                final: Any = None
                tries = _poll_tries()
                for _ in range(tries):
                    await asyncio.sleep(_POLL_INTERVAL)
                    rr = await _safe_call(
                        s,
                        "reporting-retrieve_report",
                        {"body": {"reportIds": ids}},
                        45,
                    )
                    rtext = "".join(
                        getattr(c, "text", "") for c in (rr.content or [])
                    )
                    if getattr(rr, "isError", False):
                        if "429" in rtext:
                            continue
                        return {
                            "status": "error",
                            "message": f"retrieve rejected: {rtext[:200]}",
                        }
                    try:
                        final = json.loads(rtext)
                    except Exception:
                        return {
                            "status": "error",
                            "message": (
                                "retrieve_report returned unparseable JSON."
                            ),
                        }
                    try:
                        st, fail_detail = _extract_status(final)
                    except _ApiError as exc:
                        return {
                            "status": "error",
                            "message": (
                                f"Amazon errored retrieving the {fam} "
                                f"report: {exc}"
                            ),
                        }
                    except _UnrecognizedShape as exc:
                        return {
                            "status": "error",
                            "message": (
                                f"Unrecognized retrieve_report shape — {exc}"
                            ),
                        }
                    if st in ("COMPLETED", "SUCCESS", "SUCCEEDED", "DONE"):
                        break
                    if st in ("FAILED", "ERROR", "CANCELLED"):
                        return {
                            "status": "error",
                            "message": (
                                f"Amazon report generation {st} "
                                f"({fam}/{start}..{end})"
                                + (f": {fail_detail}" if fail_detail else ".")
                            ),
                        }
                else:
                    return _pending_result(
                        account_label=account.strip() or "(explicit id)",
                        marketplace=mkt,
                        report_family=fam,
                        period=period,
                        start=start,
                        end=end,
                        waited_s=int(tries * _POLL_INTERVAL),
                    )

                url = _extract_result_url(final)
                if not url:
                    return {
                        "status": "error",
                        "message": (
                            "Report COMPLETED but no result location in "
                            "the retrieve response."
                        ),
                    }
                try:
                    async with httpx.AsyncClient(timeout=60) as hc:
                        resp = await hc.get(url)
                    if resp.status_code != 200:
                        return {
                            "status": "error",
                            "message": (
                                f"Result download HTTP {resp.status_code}."
                            ),
                        }
                    rows, totals, cols, matched = _parse_report_csv(
                        resp.content
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Ads report parse failed: %r", exc)
                    return {
                        "status": "error",
                        "message": f"Result fetch/parse failed: {exc!r}",
                    }

                if not matched:
                    # F4: do not report silent zeros. The expected metric
                    # columns are absent — surface it loudly.
                    return {
                        "status": "error",
                        "message": (
                            "Report downloaded but none of the requested "
                            "metric columns were present — cannot compute "
                            f"totals. Columns seen: {cols[:40]}"
                        ),
                    }

                acos = (
                    round(totals.get("cost", 0) / totals["sales"] * 100, 2)
                    if totals.get("sales")
                    else None
                )
                return {
                    "status": "success",
                    "account": acct_label,
                    "marketplace": mkt or None,
                    "report_family": fam,
                    "period": period,
                    "date_range": {"start": start, "end": end},
                    "account_context": "dynamic",
                    "rows": rows,
                    "columns": cols[:40],
                    "totals": totals,
                    "acos_pct": acos,
                }
    except Exception as exc:  # noqa: BLE001 — Rule 13
        from app.tools.amazon_ads_auth import AmazonAdsAuthError

        logger.error("amazon_ads_performance_report failed: %r", exc)
        if isinstance(exc, AmazonAdsAuthError):
            # Only flag reauth when LWA definitively rejected / token
            # missing. Transient → tell the bot NOT to prompt reauth.
            return {
                "status": "error",
                "message": str(exc),
                "reauth_required": bool(
                    getattr(exc, "reauth_required", False)
                ),
                "transient": bool(getattr(exc, "transient", False)),
            }
        return {
            "status": "error",
            "message": (
                f"Amazon Ads report path failed: {type(exc).__name__}: {exc}"
            ),
        }
