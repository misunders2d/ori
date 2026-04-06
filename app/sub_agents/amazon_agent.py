"""Amazon Manager sub-agent — product research, pricing, competitors, listing management.

Owns: Keepa toolset (now), Amazon SP API (future), Creatives (future).
"""

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.app_utils.models import get_model
from app.callbacks.guardrails import prompt_injection_guardrail
from app.toolsets import KeepaToolset, ScratchpadToolset, VisualizationToolset

base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
keepa_skill = load_skill_from_dir(base_dir / "keepa-skill")
scratchpad_skill = load_skill_from_dir(base_dir / "scratchpad-skill")
visualization_skill = load_skill_from_dir(base_dir / "visualization-skill")

amazon_agent = Agent(
    name="AmazonAgent",
    model=get_model("AmazonAgent"),
    description=(
        "Amazon product research and management specialist. Handles product analysis, "
        "pricing intelligence, competitor research, bestseller discovery, and listing "
        "management. Use for anything related to ASINs, SKUs, Amazon pricing, Keepa data, "
        "seller analysis, or product performance."
    ),
    instruction=(
        "You are the Amazon Manager agent — a specialist in Amazon product intelligence.\n\n"

        "YOUR TOOLS:\n"
        "- **Keepa**: Product data, pricing, sales analysis, competitor research, bestsellers. "
        "Follow the fetch→extract pattern from the keepa-skill. NEVER return raw Keepa data.\n"
        "- **Scratchpad**: For multi-ASIN research, write findings between fetches, read to synthesize.\n\n"

        "WORKFLOW:\n"
        "1. Fetch product data first (`keepa_fetch_product`), then extract what you need.\n"
        "2. For competitor analysis: use Product Finder or bestsellers to discover ASINs, "
        "then fetch and analyze each one.\n"
        "3. Always report sales as a min-max range (Keepa uses tier indicators, not exact units).\n"
        "4. Use the scratchpad when analyzing more than 3 ASINs.\n\n"

        "RULES:\n"
        "- If a tool returns an error, report it immediately. Never fabricate data or fall back to web search.\n"
        "- Check `keepa_check_tokens` before bulk operations to avoid exhausting the API budget.\n"
        "- Parent ASINs (variation parents) have NO data. Always query child ASINs.\n"
        "- BSR is shared across variations. Use `keepa_extract_sales_analysis` to compare variation performance.\n"
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[keepa_skill, scratchpad_skill, visualization_skill]),
        KeepaToolset(),
        ScratchpadToolset(),
        VisualizationToolset(),
    ],
    before_model_callback=prompt_injection_guardrail,
)
