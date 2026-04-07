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

---

# AI Image Generation (`generate_image`)

Use `generate_image` for AI-powered image creation and editing — like an AI Photoshop. This is NOT for data charts (use `generate_chart` for those).

## Three modes

| Mode | When to use | What to pass |
|------|-------------|-------------|
| **Text-to-image** | User asks you to create/draw/design something from scratch | `prompt` only |
| **Image-to-image** | User sends an image and asks you to edit/modify it | `prompt` + `base_image_path` |
| **Reference-guided** | User sends an image to edit AND reference images for style/content guidance | `prompt` + `base_image_path` + `reference_images` |

## Parameters

- `prompt` (required): Detailed description of what to generate or how to edit.
- `base_image_path`: Path to the user's uploaded image (from the `[saved to: ...]` tag in the message). Omit for pure text-to-image.
- `reference_images`: List of `{"path": "...", "description": "what this reference is for"}`. The description helps the model understand the role of each reference (e.g. "target color scheme", "desired style").
- `aspect_ratio`: `"1:1"` (default), `"1:4"`, `"1:8"`, `"2:3"`, `"3:2"`, `"3:4"`, `"4:1"`, `"4:3"`, `"4:5"`, `"5:4"`, `"8:1"`, `"9:16"`, `"16:9"`, `"21:9"`.
- `resolution`: `"1K"` (default), `"512"`, `"2K"`, `"4K"`. Higher = better quality + higher cost.
- `thinking`: `"MINIMAL"` (default), `"LOW"`, `"MEDIUM"`, `"HIGH"`. Higher = better quality but slower.

## Examples

```python
# Text-to-image
generate_image(prompt="A photorealistic product shot of a navy blue bedsheet set on a king bed, warm lighting")

# Image-to-image (edit user's uploaded photo)
generate_image(
    prompt="Change the bedsheet color to sage green, keep everything else the same",
    base_image_path="/path/from/saved_to/tag.jpg"
)

# Reference-guided editing
generate_image(
    prompt="Redecorate this bedroom using the style and color palette from the reference",
    base_image_path="/path/to/users/bedroom.jpg",
    reference_images=[
        {"path": "/path/to/style_ref.jpg", "description": "Target style and color palette"}
    ]
)
```

## Precise editing with `enhance_image_prompt`

When the user wants to change **specific parts** of an image (not regenerate the whole thing), use the two-step workflow:

1. **Call `enhance_image_prompt`** with the user's request and the image path. This analyzes the image and returns a highly detailed JSON prompt describing every aspect — colors, materials, lighting, composition, textures, spatial geometry, etc.
2. **Modify the returned prompt** — change only the parts the user asked to edit (e.g. swap a color HEX, change a material description), keeping everything else intact.
3. **Call `generate_image`** with the modified prompt + the original base image.

This ensures the AI preserves all details the user didn't ask to change.

### When to use `enhance_image_prompt`
- "Change the bedsheet color to green" → YES (partial edit)
- "Make the lighting warmer" → YES (partial edit)
- "Generate a photo of a sunset" → NO (text-to-image, just use `generate_image` directly)
- "Completely redesign this bedroom" → NO (full edit, use `generate_image` directly)

## Important

- The generated image is **automatically delivered** to the user's chat — you don't need to do anything extra.
- Write a **detailed prompt** — the more specific, the better the output.
- For image-to-image, the `base_image_path` comes from the `[saved to: ...]` tag that appears when users upload files.
- If the user sends multiple images, figure out which is the base and which are references from context.
