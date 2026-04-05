# External Research Best Practices for AI Agents

Guidelines for high-quality, token-efficient, and ethical research.

## 1. Data Fetching & Token Optimization
- **Markdown Conversion**: Raw HTML contains ~90% noise. Converting to Markdown reduces token costs and improves LLM reasoning accuracy.
- **Targeted Extraction**: Fetch only relevant page sections (e.g., `<article>`) if possible.
- **llms.txt Standard**: Always prioritize `https://repo.url/llms.txt` for a condensed documentation index.

## 2. Research Orchestration (The RRV Pattern)
- **Search**: Query multiple engines or sources to avoid vendor bias.
- **Reason**: Draft an answer by synthesizing information.
- **Verify**: Explicitly search for contradictions. If a library has been updated, old StackOverflow answers might be incorrect.

## 3. Ethics & Etiquette
- **User-Agent Identification**: Always include your agent's name and contact URL in the HTTP headers.
- **Respect robots.txt**: Honor the exclusion rules of the web.
- **Rate Limiting**: Space out requests to avoid accidental DDoS of smaller documentation sites.

## 4. Evaluation & Credibility
- **Source Scoring**: Assign higher weight to primary sources (.gov, .edu, official GitHub repos) over secondary summaries (blogs, news aggregates).
- **Date Awareness**: Always check the "Last Updated" date of a page or the "Latest Release" on GitHub.
