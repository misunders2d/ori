---
name: presentation-skill
description: "How to build PowerPoint (.pptx) decks for business reports — ads, sales, warehouse, inventory, ASIN audit, anything else. Load this skill any time the user asks for a deck, presentation, pptx, slide deck, or 'put this in a presentation'."
---

# Presentation building

You have `generate_presentation(title, slides[], filename?, template_name?)` that produces a `.pptx` file. The file is auto-attached to your reply on Slack / Telegram / A2A — no extra delivery step.

## Brand template

If `data/presentations/templates/default.pptx` exists, the tool opens it as the starting Presentation, inheriting its slide masters, color theme, fonts, and any logo placement on the master slide. Output decks then look on-brand without you wiring colors per slide.

If no template is present, the tool falls back to a blank Presentation with python-pptx default theme. The deck is still usable — you just pick layouts from the eight primitives below and the rendering is neutral.

You don't decide which path applies; the tool tells you in the response (`template_used`). Don't fabricate brand colors / logos in the absence of a template — let the default theme through.

You can also pass `template_name` explicitly if multiple templates live under `data/presentations/templates/` (e.g. `template_name="executive"` resolves to `executive.pptx`).

## Layouts — pick the one that fits the content

| Layout | When to use | Required keys | Optional |
|---|---|---|---|
| `title` | Deck cover or section divider | `title`, `subtitle` | — |
| `bullets` | Talking points, action items, takeaways | `title`, `bullets[]` | — |
| `chart` | A single chart with a one-line caption | `title`, `chart_path` | `caption` |
| `kpi_grid` | 2–6 headline metrics (revenue, units, ACoS, ROAS, conversion, etc.) | `title`, `kpis[]` (each `{label, value, delta?}`) | — |
| `table` | Comparison rows (ASIN performance, daily breakdown, top-10 keywords) | `title`, `columns[]`, `rows[][]` | — |
| `two_column` | Before/after, pros/cons, this-week-vs-last-week prose | `title`, `left`, `right` | — |
| `image` | A non-chart image (product shot, listing screenshot, photo) | `title`, `image_path` | `caption` |
| `text` | Narrative paragraph, executive summary, recommendation | `title`, `paragraph` | — |

## Composition recipes

**Weekly sales report:**
```
title("Weekly Sales — Week of {date}", subtitle="{summary one-liner}")
kpi_grid("Headline Metrics", kpis=[
  {label:"Revenue", value:"$X", delta:"+Y%"},
  {label:"Units", value:"...", delta:"..."},
  {label:"AOV", value:"...", delta:"..."},
  {label:"Conv. Rate", value:"...", delta:"..."},
])
chart("Daily Revenue", chart_path=<chart>, caption="WoW: …")
chart("Units by ASIN", chart_path=<chart>)
bullets("Notes / Anomalies", bullets=["…", "…"])
text("Action Items", paragraph="…")
```

**Ads (PPC) review:**
```
title("Ads Performance — {period}")
kpi_grid("Spend & Efficiency", kpis=[ACoS, TACoS, ROAS, CTR])
chart("Spend vs Sales Over Time", chart_path=<chart>)
table("Top Spending Campaigns", columns=[Campaign, Spend, Sales, ACoS], rows=[…])
table("Bottom Performers (>30% ACoS)", columns=[...], rows=[…])
bullets("Recommendations", bullets=["pause X", "increase bid on Y"])
```

**Warehouse / inventory:**
```
title("Inventory Snapshot — {date}")
kpi_grid("Stock Health", kpis=[Total SKUs, Days of Cover, Stranded Inv, Excess $])
chart("Days of Cover by ASIN", chart_path=<chart>)
table("ASINs at Risk (<14 days)", columns=[ASIN, Title, DoC, Suggested Reorder Qty], rows=[…])
bullets("Restock Actions This Week", bullets=["…"])
```

**ASIN deep-dive:**
```
title("ASIN Audit — {ASIN}")
kpi_grid("Last 90 Days", kpis=[Units, Revenue, Conv Rate, Buy-Box %])
chart("Sales + BSR over time", chart_path=<chart>)
chart("Price + Buy Box winner", chart_path=<chart>)
table("Top complaint clusters from reviews", columns=[Theme, Count, Sample Quote], rows=[…])
two_column("Strengths", left="…", "Weaknesses", right="…")
text("Recommendations", paragraph="…")
```

These are starting points — adapt freely. A deck can be 3 slides or 30.

## Charts go on slides via paths, not inline data

For any `chart` or `image` slide, you must pass an absolute `chart_path` / `image_path` to an existing PNG / JPG file. Generate the chart FIRST with `generate_chart` (returns `file_path`), then reference that path in the slide spec. Don't try to inline base64 bytes — the tool reads from disk.

If you need multiple charts in one deck, generate them all first, collect the paths, then call `generate_presentation` once with the full slide list. Don't call `generate_presentation` per chart.

## Hard rules

1. **Never fabricate metrics.** Every number on a slide must come from a prior tool output, user-provided data, or scratchpad content. If you don't have the data, say so — don't guess.
2. **No Drive upload is available in this build.** The deck is delivered as a chat attachment only. `GoogleWorkspaceToolset` exposes only read-only Drive tools (`drive_list_files`, `drive_download_file`); there is no `drive_upload_file` primitive. If the user says "upload this to Drive" or "save to Drive folder X," state plainly that the bot has no Drive-upload tool and offer the chat attachment instead. **Do NOT invent a tool name** — the 2026-05-20 incident proved that LLMs given a phantom tool reference will fabricate excuses ("Drive upload was bypassed as the account is not connected") rather than admit the gap.
3. **Keep slide count proportional to content.** A weekly summary doesn't need 20 slides. Five focused slides beat fifteen padded ones.
4. **Don't reuse `kpi_grid` for everything.** If the user wants narrative, use `text`. If they want a list, use `bullets`. Tile dashboards are for headline metrics only.
5. **Output filename is automatic** unless the user specifies one. If they do specify, pass it via `filename=` (the tool adds `.pptx` if missing).

## Failure handling

The tool validates slide specs upfront and returns `{"status": "error", "message": "<reason>"}` on:
- unknown layout name
- missing required key for a layout
- malformed slide spec (e.g. not a dict)
- bad template file
- write failure

If a chart path is empty or the file is missing, the slide renders WITHOUT the image (just the title) — the deck still ships. Same for `image` slides. Don't treat this as a tool failure; check the response.

## What you tell the user after generating

After `generate_presentation` succeeds, your reply is short — one line plus the attachment:

> Built `<n>`-slide deck "<title>" (template: `<default|blank>`). Attached above.

The `.pptx` is already attached via file_attachment_inject. Don't re-link, don't re-describe each slide, don't promise edits. The user opens the file to review.
