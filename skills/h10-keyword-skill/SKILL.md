---
name: h10-keyword-skill
description: "Helium10 keyword analysis protocol — Cerebro/Magnet export analysis, scoring, gaps, trends."
---

# Helium10 Keyword Analysis

You can analyze Helium10 Cerebro and Magnet CSV/XLSX exports to provide keyword intelligence. Users upload exports from the H10 web UI, and you run structured analyses.

## Workflow

1. User uploads a Cerebro or Magnet export file (CSV or XLSX)
2. Start with `keyword_summary` to understand what's in the file
3. Run the appropriate analysis based on what the user needs
4. Present findings clearly with actionable recommendations

## Tools Reference

| Tool | Purpose | When to Use |
|------|---------|-------------|
| `keyword_summary` | Quick overview — stats, columns, top keywords | Always run first on a new file |
| `analyze_keywords` | Find power keywords (high volume + opportunity) | "What are my best keyword opportunities?" |
| `find_keyword_gaps` | Keywords competitors rank for that you don't | "What keywords am I missing?" (needs multi-ASIN Cerebro) |
| `find_trending_keywords` | Keywords with rising search volume | "What's trending?", "What keywords are growing?" |
| `find_long_tail_opportunities` | Low-competition, high-conversion long phrases | "Find easy wins", "What long-tail keywords should I target?" |
| `keyword_score_report` | Full scored report with placement recommendations | "Score all keywords", "Where should I put these keywords?" |

## Cerebro vs Magnet

- **Cerebro** = Reverse ASIN lookup. Shows what keywords specific ASINs rank for. Best for competitive analysis and gap finding. Multi-ASIN mode shows your rank vs competitors.
- **Magnet** = Keyword research from seed terms. Shows related keywords and their metrics. Best for keyword discovery and expansion.

The tools auto-detect the format from column headers.

## Key Metrics Explained

| Metric | What It Means |
|--------|--------------|
| Search Volume | Estimated monthly Amazon searches |
| Search Volume Trend | % change in search volume (last 30 days) |
| IQ Score | Search Volume / Competing Products ratio — higher = better opportunity |
| CPR | Units you need to sell in 8 days to rank on page 1. Lower = easier/cheaper |
| Competing Products | Total products in search results — raw competition count |
| Title Density | Page 1 products with this keyword in their title — higher = more entrenched |
| Organic Rank | Your ASIN's position in organic results (Cerebro only) |
| Sponsored ASINs | Products running PPC ads for this keyword |
| Keyword Sales | Estimated monthly units sold via this keyword |

## Opportunity Score (0-100)

The scoring system weighs five factors:
- **Search Volume** (25%): Higher volume = more potential (log scale to avoid mega-term domination)
- **IQ Score** (20%): Higher = better demand/competition ratio
- **Trend** (15%): Growing keywords score higher
- **Competition** (20%, inverted): Fewer competitors = higher score
- **CPR Feasibility** (20%, inverted): Lower CPR = more achievable

## Placement Recommendations

Based on opportunity score:
- **Title** (75+): Primary keywords — front-load in listing title
- **Bullets** (50-74): Secondary keywords — one per bullet point
- **Backend** (25-49): Search terms / hidden keywords
- **PPC Only** (<25): Test via advertising before investing in listing optimization

## Gap Analysis

For gap analysis, the user needs a **multi-ASIN Cerebro export**:
1. Enter their ASIN + 5-10 competitor ASINs in Cerebro
2. Run the search and export
3. Upload the export

The `find_keyword_gaps` tool identifies keywords where competitors rank well (avg rank <= 50) but the user's ASIN doesn't rank (rank > 100 or missing).

## Presentation Tips

- Always show keyword, search volume, and the most relevant metric for the analysis
- For large result sets, focus on the top 20-30 and mention the total count
- Round scores to one decimal place
- When presenting gaps, emphasize the competitor rank to show "they're on page 1, you're not"
- For trending keywords, show the trend % prominently
- Suggest next steps: "These 5 keywords should go in your title", "Test these via PPC first"
- If the user wants visualization, use the visualization tools to create charts

## Common User Requests → Tool Mapping

| User Says | Tool to Use |
|-----------|-------------|
| "Analyze this file" | `keyword_summary` then `analyze_keywords` |
| "What keywords am I missing?" | `find_keyword_gaps` |
| "What's trending?" | `find_trending_keywords` |
| "Find easy wins" | `find_long_tail_opportunities` |
| "Score all my keywords" | `keyword_score_report` |
| "Where should I put these keywords?" | `keyword_score_report` (has placement) |
| "Compare me to competitors" | `find_keyword_gaps` + `analyze_keywords` |
