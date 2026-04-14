# Account Health Reference

## Tool

### `sp_get_account_health(days=30, max_wait_seconds=90)`
One-shot wrapper over the `GET_V2_SELLER_PERFORMANCE_REPORT` report. Requests, polls, downloads, parses, and returns a compact digest — you don't manage the report lifecycle manually.

## What comes back

```
{
  "status": "success",
  "overall_status": "GOOD" | "AT_RISK" | "CRITICAL" | ...,
  "healthy_sections": [...],         # metric names with status = GOOD
  "issues": [                         # only non-GOOD metrics
    {"section": "productAuthenticity", "status": "CRITICAL", "details": {...}}
  ],
  "issue_count": N,
  "message": "human-readable one-liner",
  "report_id": "..."
}
```

If the report doesn't finish within `max_wait_seconds`, the tool returns `{"status": "pending", "report_id": "..."}` — you can follow up with `sp_check_report` and `sp_download_report` manually.

## When to use it

Whenever the user asks about:
- **Account Health Rating (AHR)**
- **Policy violations / warnings / suspension risk**
- **Product authenticity complaints, listing policy violations, intellectual property complaints**
- **Shipping / late shipment / cancellation / VTR metrics** from the Account Health dashboard
- Anything phrased as "am I in trouble?" or "is my account healthy?"

Don't use `sp_request_report` for this — use the wrapper. It's purpose-built and returns a much friendlier shape.

## Interpreting the digest

- `overall_status == "GOOD"` with empty `issues` → nothing to worry about; say so and move on.
- `overall_status == "GOOD"` but `issues` non-empty → some metric is slipping even though the overall rollup is still fine. Surface the issue so the user can fix it before it escalates.
- `overall_status` anything else → lead with what's wrong and summarize the top 1–2 `issues` with their details. Don't dump the whole dict; humans care about what to do next.

## Adjacent capability: push notifications

Amazon has an `ACCOUNT_STATUS_CHANGED` notification type (via the Notifications API) that fires on NORMAL ↔ AT_RISK ↔ DEACTIVATED transitions. It requires AWS SNS/SQS infrastructure to receive and is not currently wired up here. The simpler substitute is scheduling a recurring `sp_get_account_health` poll — fast enough for account-health response times (which don't change minute-to-minute).

## Live references

- [GET_V2_SELLER_PERFORMANCE_REPORT schema](https://developer-docs.amazon.com/sp-api/docs/report-type-values-performance)
- [SP-API blog: Ensuring healthy seller account status](https://developer-docs.amazon.com/sp-api-blog/docs/ensuring-healthy-seller-account-status)
