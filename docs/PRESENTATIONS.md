# Presentation generation (.pptx)

PowerPoint deck generation lives in `app/tools/presentations.py` and is mounted on `AmazonDataAnalystAgent` via `PresentationToolset` (`app/toolsets/presentations.py`). Output `.pptx` files are auto-attached to the agent's response via the existing file-attachment plumbing in `app/callbacks/guardrails.py` (capture + inject) — no transport-specific wiring needed for Slack / Telegram / A2A delivery.

## Why python-pptx, not Google Slides API

Two reasons:

1. **No new OAuth scope.** Slides API would require re-authing every Google Workspace user with the new `https://www.googleapis.com/auth/presentations` scope. python-pptx generates a standalone file with zero credentials — works for any caller, including A2A peers that don't have Google Workspace at all.
2. **Brand template story is simpler.** With python-pptx, drop a branded `.pptx` under `data/presentations/templates/`, and every deck inherits its master slides, color theme, fonts, and logo placement. The Slides API's theme story is less ergonomic and harder to keep in lockstep with a real PowerPoint template.

Trade-off: output decks are not live-editable in a browser. Users open them in PowerPoint / Keynote / Google Slides (via upload) and edit there. We judged that fine for the report-deck use case (weekly sales, ads review, inventory snapshot, ASIN audit). If "edit in browser live" becomes required, add a `slides_create_*` path separately — they coexist cleanly.

## Brand template behaviour

Lookup order (first match wins):
1. `data/presentations/templates/<template_name>.pptx` — when `template_name` is explicitly passed
2. `data/presentations/templates/default.pptx` — implicit fallback
3. **None** → blank `Presentation()` with python-pptx defaults

The tool surfaces which path was taken in `result["template_used"]` (either the absolute path or the literal string `"blank"`). The skill (`skills/presentation-skill/SKILL.md`) tells the agent to NOT fabricate brand colors when no template is found — let the default theme through.

A bad template file (corrupt, non-pptx) is a HARD error: the tool refuses to silently fall back. The user must fix or remove the file. We picked loud-fail because silent fallback hid brand drift in similar tools we've watched before.

## Slide vocabulary (eight layouts)

| Layout | Purpose | python-pptx layout fallback |
|---|---|---|
| `title` | Cover / section divider | layout 0 (Title) |
| `bullets` | Talking points, action items | layout 1 (Title + Content) |
| `chart` | Single chart + caption | layout 5 (Title Only) |
| `kpi_grid` | 2-6 headline metric tiles | layout 5 (Title Only) + positioned text boxes |
| `table` | Tabular comparison | layout 5 + `add_table` |
| `two_column` | Before/after, pros/cons | layout 5 + two positioned text boxes |
| `image` | Non-chart image | layout 5 + `add_picture` |
| `text` | Narrative paragraph | layout 1 (Title + Content) |

A `kpi_grid` auto-arranges its tiles into a 2×N or 3×N grid based on the number of KPIs (≤2 → single row, 3-4 → 2×2, 5-6 → 3×2). Delta values prefixed with `+` / `▲` render green; `-` / `▼` render red; neutral grey. Coloring intentionally simple — no need to think about Mellanni brand palette for ad-hoc data.

The layout vocabulary is **domain-agnostic** by design. Same eight primitives compose an ads-performance review, a weekly sales report, a warehouse inventory snapshot, an ASIN audit, and a quarterly strategy deck — content varies, layout types reuse.

## File output

Path: `data/exports/presentations/<slug-of-title>-<UTC-timestamp>-<rand>.pptx` unless `filename` is passed (in which case `data/exports/presentations/<filename>.pptx`).

The path goes through `file_attachment_capture` (mounted on AmazonDataAnalystAgent's `after_tool_callback`), which stashes it in `state["__pending_file_parts__"]`. Coordinator's `after_model_callback` (`file_attachment_inject`) drains the queue and inlines the bytes as a `Part(inline_data=Blob(...))` with a `__contract_file:<path>` display_name marker. ADK's A2A converter renders that as a `FilePart` for A2A peers; the Slack / Telegram pollers dedupe against the marker and attach the bytes once.

20 MB attachment cap applies (same as `_FILE_ATTACHMENT_MAX_BYTES`). Decks well under that for any reasonable slide count + chart embeds; raise the cap before generating 100-slide decks.

## No Drive upload by default

Decks ship as chat attachments and stop there. If the user wants the file in Drive, they ask explicitly and the agent calls `drive_upload_file` separately. We didn't bundle the upload into `generate_presentation` because (a) most decks are throwaway and Drive clutter is real, and (b) the auth/scope path for Drive is separate from PPTX generation. Two clean tools beat one tangled one.

## Failure modes

The tool validates slide specs UPFRONT before any rendering. Errors surface as `{"status": "error", "message": ...}` with the slide index + the offending field, so the agent retries with a corrected spec rather than getting a half-written `.pptx`.

A chart / image slide with a missing file_path renders the slide WITHOUT the image (just title + optional caption). Not an error — the deck still ships. The agent decides whether to flag this to the user or just absorb it.

## When to use what

Skill (`skills/presentation-skill/SKILL.md`) carries the full recipes for weekly sales, ads review, warehouse, ASIN deep-dive, etc. — agent loads the skill on `present|deck|pptx|slides|powerpoint` triggers and picks recipes from there.

## Adding a brand template

Drop a `.pptx` file at `data/presentations/templates/default.pptx`. The file should have:
- Slide masters with the brand color palette baked in
- Font choices on the master slide (titles + body)
- Logo / footer placement on the master if branded
- A few representative slide layouts (Title, Title+Content, Title-Only) — python-pptx picks among them by index

We don't ship a default template in the repo because branding varies per deployment. The folder is `.gitkeep`-only so a `git pull` doesn't overwrite a user-provided template.
