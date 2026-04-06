"""Amazon Manager sub-agent — product research, pricing, competitors, listing management.

Owns: Keepa toolset, Amazon SP API, Creatives (future).
"""

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.app_utils.models import get_model
from app.callbacks.guardrails import prompt_injection_guardrail
from app.toolsets import KeepaToolset, ScratchpadToolset, VisualizationToolset
from app.toolsets.sp_api import SPApiToolset

base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
keepa_skill = load_skill_from_dir(base_dir / "keepa-skill")
sp_api_skill = load_skill_from_dir(base_dir / "sp-api-skill")
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
        "- **SP-API**: Direct Amazon Selling Partner API — catalog items, listing details, "
        "competitive pricing, and reports. Use sp_get_catalog_item for product attributes, "
        "sp_get_listing for your SKU details, sp_get_competitive_pricing for price comparisons.\n"
        "- **SP-API Reports**: Request any Amazon report type with sp_request_report, "
        "check status with sp_check_report, download with sp_download_report.\n"
        "- **Scratchpad**: For multi-ASIN research, write findings between fetches, read to synthesize.\n\n"

        "WORKFLOW:\n"
        "1. For product research: use Keepa for historical data, SP-API for current listing details.\n"
        "2. For competitor analysis: use Product Finder or bestsellers to discover ASINs, "
        "then fetch and analyze each one.\n"
        "3. Always report sales as a min-max range (Keepa uses tier indicators, not exact units).\n"
        "4. Use the scratchpad when analyzing more than 3 ASINs.\n"
        "5. For reports: request → wait 30-60s → check status → download when DONE.\n\n"

        "RULES:\n"
        "- If a tool returns an error, report it immediately. Never fabricate data or fall back to web search.\n"
        "- Check `keepa_check_tokens` before bulk operations to avoid exhausting the API budget.\n"
        "- Parent ASINs (variation parents) have NO data. Always query child ASINs.\n"
        "- BSR is shared across variations. Use `keepa_extract_sales_analysis` to compare variation performance.\n"
        "- SP-API has strict rate limits. If you get a throttling error, wait and retry. "
        "Never spam retries — use progressive backoff.\n"
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[keepa_skill, sp_api_skill, scratchpad_skill, visualization_skill]),
        KeepaToolset(),
        SPApiToolset(),
        ScratchpadToolset(),
        VisualizationToolset(),
    ],
    before_model_callback=prompt_injection_guardrail,
)
