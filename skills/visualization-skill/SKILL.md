---
name: visualization-skill
description: "Data visualization protocol — write plotting code, execute in sandbox, deliver as image or interactive HTML."
---

# Data Visualization Skill

You can generate charts by writing Python plotting code. The `generate_chart` tool executes your code in a sandboxed environment and saves the output for delivery to the user.

## Available Libraries

| Library | Best for | Output |
|---------|----------|--------|
| **matplotlib** | Quick static charts, line/bar/scatter, subplots | PNG |
| **seaborn** | Statistical plots, heatmaps, distributions | PNG |
| **plotly** | Interactive charts the user can zoom/hover | HTML |

All three are available. Pick based on what the user needs — default to matplotlib for speed, plotly for interactivity.

## Procedure

- [ ] Step 1: **Prepare data** — extract from scratchpad, tool output, or BigQuery results
- [ ] Step 2: **Write code** — standard Python. You have: `plt`, `np`, `matplotlib`, `datetime`, `json`, `math`. Import `seaborn`, `pandas`, `plotly` as needed.
- [ ] Step 3: **Save output** — use `OUTPUT_PATH` (pre-set variable):
  - matplotlib: `plt.savefig(OUTPUT_PATH)` (auto-saved if you forget)
  - plotly: `fig.write_html(OUTPUT_PATH)` or `fig.write_image(OUTPUT_PATH)`
- [ ] Step 4: **Call** `generate_chart(code=your_code, filename="descriptive_name.png")`

The file is automatically attached to your response and sent to the user's chat.

## Example

```python
generate_chart(
    filename="sales_trend.png",
    code='''
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

dates = [datetime(2026, 3, d) for d in range(1, 31)]
sales = [120, 135, 110, ...]  # from scratchpad or tool output

fig, ax = plt.subplots(figsize=(12, 5))
ax.plot(dates, sales, color="#2196F3", linewidth=2)
ax.fill_between(dates, sales, alpha=0.1, color="#2196F3")
ax.set_title("Daily Sales — March 2026", fontsize=14, fontweight="bold")
ax.set_ylabel("Units")
ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
plt.savefig(OUTPUT_PATH, dpi=150)
'''
)
```

## Gotchas

- **OUTPUT_PATH is pre-set** — don't hardcode file paths. Always use `OUTPUT_PATH`.
- **Use .png for matplotlib/seaborn, .html for plotly** — match the filename extension to the library.
- **No file system access** — the sandbox restricts imports. You can't read files, make HTTP calls, or access os/sys.
- **Data must be inline** — embed the data directly in the code (from scratchpad_read, tool results, etc.). The sandbox can't access session state or tools.
- **Large datasets** — if plotting hundreds of points, aggregate first. Don't dump 10,000 rows into the code string.
- **Clean aesthetics** — use readable fonts, clear labels, proper axis formatting. The user will see this in a chat window — make it count.
- **Auto-save fallback** — if you forget `plt.savefig()`, matplotlib figures are auto-saved. But always be explicit.
