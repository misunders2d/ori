# Token Budget, Domains, and Limits

## Token costs per endpoint

| Tool | Endpoint | Cost |
|---|---|---|
| `keepa_check_tokens` | `/token` | **0** |
| `keepa_fetch_product` | `/product` (with `offers=20`) | **3** (1 base + 2 for offers) |
| `keepa_extract_*` | (cache-only, no API call) | **0** |
| `keepa_product_finder` | `/query` | ~1 |
| `keepa_get_categories` | `/category` | ~1 |
| `keepa_get_bestsellers` | `/bestsellers` | ~1 |
| `keepa_get_seller_info` | `/seller` | ~1 |
| `keepa_get_top_sellers` | `/topseller` | **50** |

Costs are per call; bulk endpoints (e.g., seller lookups for multiple seller IDs) usually charge per item, not per call.

**Subscription model:** tokens generate at a fixed rate per minute (depends on your plan tier). Unused tokens expire 60 minutes after creation, so banking them across long idle stretches doesn't work. The toolset enforces a 5-token reserve before any fetch — at low balances, you'll get an explicit `"Keepa token budget exhausted"` error with the refill ETA.

## Refill rate

`keepa_check_tokens()` returns `refill_rate_per_min`. To estimate "can I fetch N products in the next minute?" check: `tokens_left + refill_rate_per_min ≥ 3 * N + 5_reserve`.

## Domain IDs (Amazon locales)

| ID | Marketplace |
|---:|---|
| 1 | US (`amazon.com`) |
| 2 | GB (`amazon.co.uk`) |
| 3 | DE (`amazon.de`) |
| 4 | FR (`amazon.fr`) |
| 5 | JP (`amazon.co.jp`) |
| 6 | CA (`amazon.ca`) |
| 7 | (reserved — was CN, deprecated) |
| 8 | IT (`amazon.it`) |
| 9 | ES (`amazon.es`) |
| 10 | IN (`amazon.in`) |
| 11 | MX (`amazon.com.mx`) |
| 12 | BR (`amazon.com.br`) — supported by Keepa, not verified in this toolset |

US (`domain=1`) is the default for every tool. **Never use 0 or 7** — they're reserved/deprecated and will return errors.

`keepa_get_top_sellers` does **not** support China — Keepa's API explicitly excludes that endpoint there.

## Cache TTL

- Per-ASIN raw response cached at `tmp/keepa_cache/{ASIN}.json`
- TTL: **1 hour** from fetch
- After TTL expiry, `keepa_fetch_product` re-hits the API automatically (3 tokens again)
- If the user explicitly says "use fresh data" or "ignore the cache," they're overriding the TTL — but the toolset has no force-refresh flag, so the only way is to wait an hour or delete the cache file out-of-band

## `offers` parameter constraint

Keepa requires `offers` to be either omitted or **between 20 and 100**. The toolset always uses `offers=20` (the cheapest option that still gives buy box + live offer data). Values between 1 and 19 are not allowed by the API.

## Rate limits beyond tokens

Keepa doesn't impose request-rate limits beyond the token bucket — if you have tokens, requests go through. The bottleneck is always token budget, not RPS. But for very large batches:

- Run `keepa_check_tokens` first
- Sequence fetches with at most ~10 in flight (the toolset uses `httpx.AsyncClient`; concurrent calls share the same token pool, so over-parallelizing just causes "budget exhausted" errors halfway through)

## When to NOT use Keepa

- **Your own listings' real-time stock or sales** → use SP-API (`sp_get_inventory_summaries`, `sp_list_orders`). Keepa shows public market data, not your seller-account internals.
- **Your own competitive pricing** → SP-API `sp_get_competitive_pricing` is faster and free of Keepa's token cost for ASINs you already track.
- **Account-level fees / settlement** → SP-API only.
- **PPC / advertising data** → not in Keepa at all.
