---
name: ads-reporting
description: Create and retrieve Amazon Ads reports. Use this when the user asks for ad performance data, campaign metrics, or reporting.
version: 1.0.0
visibility: public
---

# Ads Reporting Skill

This skill creates and retrieves Amazon Ads reports using reporting tools.

## Preferred path (Ori)

For most performance asks, call the deterministic `amazon_ads_performance_report`
tool (account / marketplace / period / report family params) instead of
hand-writing payloads — it builds, polls, retrieves, and parses for you.
Only hand-build `reporting-*` payloads for asks it does not cover. The
live-verified payload contracts (period/timezone/account-context/family
schemas/retrieve/throttling) are in
[payload-recipes.md](references/payload-recipes.md) — read it before
constructing any payload by hand.

## Prerequisites

Before executing, verify that ALL of the following MCP tools are available. The MCP server prefix may vary — match by the tool name suffix:
- `reporting-create_report`
- `reporting-retrieve_report`

If any tool is missing, STOP and tell the user which tools are unavailable and that this skill cannot execute. Do NOT proceed with partial execution.

## Workflow

1. **Gather missing parameters** — Use the `AskUserQuestion` tool to interactively ask the user for any required information they haven't provided. Ask for all missing parameters in a single question when possible. Required parameters:
   - **Account ID** — if not provided or ambiguous
   - **Date range** — always confirm before creating a report
   - **Metrics/fields** — if the user hasn't specified what data they want, propose sensible defaults and ask for confirmation
   - **Filters** — if the user mentions specific campaigns, countries, or ad products, confirm the filter values
2. **Create the report** — Call `reporting-create_report` with the resolved IDs, fields, filters, and date range.
3. **Poll for completion** — Call `reporting-retrieve_report` with exponential backoff (start at 30s, double each retry) until the report status is `SUCCESSFUL` or a terminal failure state.

**Important:** All tool calls in this workflow must include the `skill` object: `{ "skillName": "ads-reporting", "version": "1.0.0" }`.

## Rules

### Skill Object

- When calling any tool from this skill, always pass the `skill` object: `{ "skillName": "ads-reporting", "version": "1.0.0" }`.
- Do NOT populate the `skill` object if a tool is called outside of a skill context (e.g., direct user invocation without a skill). The `skill` field is only for tracking which skill triggered the tool call.

### Account IDs

- `advertiserAccountId` must be either:
  - A global advertiser ID starting with `amzn1.ads-account.g.`
  - A numeric DSP account ID
- `managerAccountId` starts with `amzn1.ads1.ma1.`

### Date Range

- Always use the `AskUserQuestion` tool to confirm the desired date range with the user before creating a report.
- Use ISO 8601 date format (YYYY-MM-DD).

### Fields and Filters

For the complete list of valid fields, metrics, and filters, see [fields.md](references/fields.md). Key rules:
- `dateRange.value` is required and must always be included in `fields`
- `budgetCurrency.value` is required and must always be included in `fields`

## Example

**User:** "Show me impressions and clicks for campaign 12345 in the US for last week."

**Steps:**
1. Use `AskUserQuestion` to confirm date range (e.g., "last week") and any other missing details.
2. Call `reporting-create_report` with:
   - `skill`: `{ "skillName": "ads-reporting", "version": "1.0.0" }`
   - `fields`: `["dateRange.value", "campaign.id", "campaign.name", "metric.impressions", "metric.clicks", "metric.ctr"]`
   - `filters`: `[{"field": "campaign.id", "values": ["12345"]}]`
   - Appropriate date range for "last week"
3. Poll `reporting-retrieve_report` with exponential backoff (start at 30s) until complete.
