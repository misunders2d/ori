# Chart Recipes — Ready-to-Use Code for generate_chart()

## Sales Trend Line

```python
generate_chart(
    filename="sales_trend.png",
    code='''
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

# Data from scratchpad or tool output
dates = [datetime(2026, 3, d) for d in range(1, 31)]
sales = [120, 135, 110, 145, 160, 155, 170, 180, 175, 190,
         200, 195, 210, 220, 215, 230, 225, 240, 235, 250,
         245, 260, 255, 270, 265, 280, 275, 290, 285, 300]

fig, ax = plt.subplots(figsize=(12, 5))
ax.plot(dates, sales, color="#2196F3", linewidth=2, marker='o', markersize=3)
ax.fill_between(dates, sales, alpha=0.1, color="#2196F3")
ax.set_title("Daily Sales — March 2026", fontsize=14, fontweight="bold")
ax.set_ylabel("Units Sold")
ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
plt.xticks(rotation=45)
plt.tight_layout()
plt.savefig(OUTPUT_PATH, dpi=150)
'''
)
```

## Competitor Pricing Bar Chart

```python
generate_chart(
    filename="competitor_pricing.png",
    code='''
import matplotlib.pyplot as plt
import numpy as np

products = ["Our Product", "Competitor A", "Competitor B", "Competitor C", "Competitor D"]
prices = [29.97, 27.99, 31.50, 28.95, 33.99]
colors = ["#4CAF50" if p == min(prices) else "#2196F3" if i == 0 else "#9E9E9E"
          for i, p in enumerate(prices)]

fig, ax = plt.subplots(figsize=(10, 5))
bars = ax.bar(products, prices, color=colors, edgecolor="white", linewidth=1.5)

for bar, price in zip(bars, prices):
    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
            f"${price:.2f}", ha="center", fontweight="bold")

ax.set_title("Competitive Pricing Comparison", fontsize=14, fontweight="bold")
ax.set_ylabel("Price ($)")
ax.set_ylim(0, max(prices) * 1.15)
plt.tight_layout()
plt.savefig(OUTPUT_PATH, dpi=150)
'''
)
```

## ACoS / ROAS Dual-Axis Chart

```python
generate_chart(
    filename="acos_roas_trend.png",
    code='''
import matplotlib.pyplot as plt
import numpy as np

weeks = ["W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8"]
acos = [0.35, 0.32, 0.28, 0.25, 0.27, 0.23, 0.21, 0.19]
spend = [500, 550, 600, 650, 700, 720, 750, 780]
revenue = [s/a for s, a in zip(spend, acos)]

fig, ax1 = plt.subplots(figsize=(12, 5))
ax2 = ax1.twinx()

ax1.bar(weeks, spend, alpha=0.3, color="#2196F3", label="Ad Spend ($)")
ax1.bar(weeks, revenue, alpha=0.3, color="#4CAF50", label="Ad Revenue ($)")
ax2.plot(weeks, [a*100 for a in acos], color="#F44336", linewidth=2, marker="o", label="ACoS %")

ax1.set_ylabel("Dollars ($)")
ax2.set_ylabel("ACoS (%)", color="#F44336")
ax1.set_title("Advertising Performance Trend", fontsize=14, fontweight="bold")

lines1, labels1 = ax1.get_legend_handles_labels()
lines2, labels2 = ax2.get_legend_handles_labels()
ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right")

plt.tight_layout()
plt.savefig(OUTPUT_PATH, dpi=150)
'''
)
```

## Keyword Distribution Histogram

```python
generate_chart(
    filename="keyword_distribution.png",
    code='''
import matplotlib.pyplot as plt
import numpy as np

# Search volume distribution from H10 data
volumes = [50, 100, 200, 300, 500, 800, 1000, 1500, 2000, 3000,
           5000, 8000, 10000, 15000, 20000, 50000, 100000]
bins = [0, 100, 500, 1000, 5000, 10000, 50000, 200000]

fig, ax = plt.subplots(figsize=(10, 5))
ax.hist(volumes, bins=bins, color="#2196F3", edgecolor="white", linewidth=1.5)
ax.set_xscale("log")
ax.set_title("Keyword Search Volume Distribution", fontsize=14, fontweight="bold")
ax.set_xlabel("Monthly Search Volume (log scale)")
ax.set_ylabel("Number of Keywords")
plt.tight_layout()
plt.savefig(OUTPUT_PATH, dpi=150)
'''
)
```

## Pie Chart — Revenue by Variation

```python
generate_chart(
    filename="variation_revenue.png",
    code='''
import matplotlib.pyplot as plt

variations = ["King Navy", "Queen White", "King Gray", "Full Navy", "Other"]
revenue = [45000, 32000, 18000, 12000, 8000]
colors = ["#2196F3", "#4CAF50", "#FF9800", "#9C27B0", "#9E9E9E"]
explode = (0.05, 0, 0, 0, 0)

fig, ax = plt.subplots(figsize=(8, 8))
wedges, texts, autotexts = ax.pie(
    revenue, labels=variations, colors=colors, explode=explode,
    autopct="%1.1f%%", startangle=90, textprops={"fontsize": 11}
)
for autotext in autotexts:
    autotext.set_fontweight("bold")

ax.set_title("Revenue Share by Variation", fontsize=14, fontweight="bold")
plt.tight_layout()
plt.savefig(OUTPUT_PATH, dpi=150)
'''
)
```

## Interactive Plotly Chart (HTML output)

```python
generate_chart(
    filename="interactive_sales.html",
    code='''
import plotly.graph_objects as go

months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun"]
organic = [15000, 17000, 19000, 22000, 24000, 26000]
ppc = [5000, 6000, 7000, 7500, 8000, 8500]

fig = go.Figure()
fig.add_trace(go.Bar(name="Organic", x=months, y=organic, marker_color="#4CAF50"))
fig.add_trace(go.Bar(name="PPC", x=months, y=ppc, marker_color="#2196F3"))

fig.update_layout(
    title="Monthly Revenue: Organic vs PPC",
    barmode="stack",
    yaxis_title="Revenue ($)",
    template="plotly_white"
)
fig.write_html(OUTPUT_PATH)
'''
)
```
