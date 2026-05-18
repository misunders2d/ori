# Amazon Ads reporting — payload recipes (live-verified)

Operational truth for building Amazon Ads report payloads in Ori.
Verified live against the hosted MCP on 2026-05-17. Prefer the
deterministic tool below — only hand-build payloads for asks it does not
cover.

## 0. Prefer the deterministic helper

Ori exposes `amazon_ads_performance_report` (FunctionTool on
`AmazonAgent`). Use it for almost every performance ask — it builds the
nested payload, runs create → poll → retrieve, parses the result, and
returns totals. Do **not** hand-write `reporting-*` payloads when this
tool fits.

Params: `account` (name substring — **required**, no default) or
`advertiser_account_id`; `marketplace` (ISO, default `US`); `period`
(`yesterday` default | `today` | `last_7_days` | `last_30_days` |
`this_month` | `last_month` | `custom`); `start_date`/`end_date` (for
`custom`); `report` (`campaign` default | `product` | `inventory` |
`custom`; `targeting` is disabled until its fields are verified);
`timezone` (default `America/Los_Angeles`); `fields` (only for
`report='custom'`). The account name the user gives is matched
case-insensitively; an ambiguous or no match returns a count-only error
(account names are never echoed back).

Examples (substitute the account name the user actually names):
- "<account> US yesterday ad performance" → `account='<account>'`.
- "last 7 days US campaign performance for <account>" →
  `account='<account>', period='last_7_days'`.
- "this month so far" → `period='this_month'`.
- "May 1–10 custom" → `period='custom', start_date='2026-05-01',
  end_date='2026-05-10'`.

Fall back to raw `reporting-*` MCP tools + the recipes below only when
the ask needs fields/filters/grouping the helper does not model.

## 1. Timezone

Naked day references ("yesterday", "today", "last week") are
**America/Los_Angeles** (user is Pacific) unless the user names another
tz. Resolve the date in that tz, then send ISO `YYYY-MM-DD`.

## 2. Account context

- **Dynamic (default):** only `Amazon-Ads-ClientId` + `Authorization`
  headers; account identifiers go in the request body
  (`accessRequestedAccounts`). This is what Ori uses.
- **Fixed:** vault `ADS_API_ACCOUNT_SELECTION_MODE=FIXED` +
  `Amazon-Ads-AI-Account-Selection-Mode: FIXED` header + one of
  profileId / accountId / managerAccountId. Off by default.
- `accessRequestedAccounts[]` strict oneOf: `advertiserAccountId` **or**
  `managerAccountId` — `profileId` is rejected.
- `advertiserAccountId` = global id `amzn1.ads-account.g.…` (or numeric
  DSP id). Enumerate with
  `account_management-query_advertiser_account` (body `{}`) or
  `ads_accounts-list_ads_accounts`.
- **Auth caveat:** the token can *enumerate* more accounts than it can
  *report on*. An unauthorized `advertiserAccountId` →
  `"Multi-account authorization failed"`. Try another account; only a
  subset is report-authorized.

## 3. Report families (two different body schemas)

### Query family — `reporting-create_report` (campaign / targeting / custom)

```json
{
  "accessRequestedAccounts": [{"advertiserAccountId": "amzn1.ads-account.g.…"}],
  "reports": [{
    "format": "CSV",
    "currencyOfView": "USD",
    "periods": [{"datePeriod": {"startDate": "YYYY-MM-DD", "endDate": "YYYY-MM-DD"}}],
    "query": {
      "fields": ["budgetCurrency.value", "dateRange.value", "campaign.id",
                 "campaign.name", "metric.impressions", "metric.clicks",
                 "metric.totalCost", "metric.sales"],
      "filter": {"on": {"comparisonOperator": "EQUALS",
                        "field": "campaign.country", "not": false,
                        "values": ["US"]}}
    }
  }]
}
```
- `budgetCurrency.value` + `dateRange.value` are **required** in `fields`
  whenever metrics are requested.
- Marketplace scoping: filter `campaign.country` (ISO code) — the valid
  country-scope field (`profile.id`, `marketplace.id`,
  `campaign.countryCode`, `campaign.countries` are all rejected).
- Probe unknown fields by adding to `fields` — error `400005` names the
  bad field. `target.id` is NOT valid for targeting (probe real names;
  full catalog in `fields.md`).

### No-query family — `reporting-create_product_report` / `…-create_inventory_report`

`reports[0]` requires `format` + `periods`; **`query` is forbidden**
(`additionalProperties:false`). No `fields`/`filter` block — the family
is implied by the tool. Marketplace filter does not apply here.

```json
{
  "accessRequestedAccounts": [{"advertiserAccountId": "…"}],
  "reports": [{"format": "CSV", "currencyOfView": "USD",
               "periods": [{"datePeriod": {"startDate": "…", "endDate": "…"}}]}]
}
```

## 4. Retrieve

`reporting-retrieve_report` body = `{"reportIds": ["<id>", …]}` (array of
strings, required). NOT `reportId` / `id` / `reports[]`. The id comes
from the create response. Poll until status is `COMPLETED`/`SUCCESS`;
then a result location (URL) appears — download & parse the CSV (may be
gzipped). Never log/echo the presigned URL.

The helper polls for a production-sensible budget (default **600 s**,
clamped 60–1200 s, vault key `ADS_API_REPORT_POLL_SECONDS`, 15 s
interval). If still generating at the budget it returns
`{"status": "pending", "message": …, "retry": {account, marketplace,
report_family, period, date_range}}` — a distinct non-error status with
ONLY the deterministic request params (no reportId, no URL, no account
id). The bot relays the message and can resume simply by re-issuing the
same request (deterministic — no data lost). Terminal `FAILED` surfaces
`failureReason`.

## 4b. Response envelope (verified live 2026-05-18)

`reporting-create_report` and `reporting-retrieve_report` wrap their
result in an envelope:

```json
{"error": null,
 "success": [{"index": 0, "report": {
     "reportId": "...", "status": "PENDING|COMPLETED|FAILED",
     "failureCode": null, "failureReason": null,
     "completedReportParts": [{"url": "https://…"}],
     "periods": [...], "query": {...}, "format": "CSV"}}]}
```

- `error` non-null → Amazon rejected the call; the message is the real
  failure (relay it; mask account ids). Do NOT report a generic "no
  reportId".
- `error` null → real payload is `success` (a **list** of
  `{index, report}`). `reportId`/`status` live at `success[i].report.*`.
- Result file URL(s): `success[i].report.completedReportParts[].url`
  (present once `status == COMPLETED`).
- `query_advertiser_account` is **not** enveloped — `advertiserAccounts`
  is top-level. Unwrap defensively but expect either.

The helper handles all of this and fails loud (`_ApiError` for an
Amazon-reported error, `_UnrecognizedShape` for an unknown shape) — never
silently mis-reads.

## 5. Throttling

Amazon's gateway returns HTTP `429 Too Many Requests` (openresty) under
rapid repeated `create_report`. Back off ~2–4 min; do not hammer.

## 6. Non-reporting tool body contracts (live matrix 2026-05-18)

- **No `body` at all** (call empty args; `{"body":{}}` rejected):
  `manager_accounts-get_manager_accounts`, `ads_accounts-get_ads_account`.
- **`body: {}` accepted**: `account_management-query_advertiser_account`,
  `ads_accounts-list_ads_accounts`.
- **Singular `accessRequestedAccount`** (object, NOT the reporting plural
  `accessRequestedAccounts` array) required by:
  `campaign_management-query_campaign` / `-query_ad_group` /
  `-query_ad` / `-query_ad_association` / `-query_target` /
  `-check_product_eligibility`, `eligibility-product_list` /
  `eligibility-programs`.
- **`userId` required**: `user_permissions-list_user_permissions`,
  `user_roles-list_user_roles`.
- `users-list_users` → `Unauthorized` (token lacks the users scope).

`account_management-query_advertiser_account` response shape:
`{"advertiserAccounts":[{"advertiserAccountId","displayName",
"isGlobalAccount","alternateIds":[{countryCode,entityId,profileId}]}]}`.
Account name = **`displayName`**. `alternateIds[].profileId` is the
per-marketplace profile (use `campaign.country` filter for marketplace
scoping in reports, not profileId).

## 7. skill object

When invoked under this skill, pass
`skill: {"skillName": "ads-reporting", "version": "1.0.0"}` on tool
calls. Omit for direct/non-skill invocations.
