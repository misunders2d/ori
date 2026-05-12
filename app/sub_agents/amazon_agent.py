"""Amazon Manager sub-agent — product research, pricing, competitors, listing management.

Owns: Keepa toolset, Amazon SP API, Creatives (future).
"""

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.app_utils.models import get_model
from app.callbacks.guardrails import (
    file_attachment_capture,
    file_attachment_inject,
    prompt_injection_guardrail,
    tool_output_spillover_guardrail,
)
from app.toolsets import KeepaToolset, ScratchpadToolset
from app.toolsets.sp_api import SPApiToolset
from app.toolsets.h10 import H10Toolset

base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
keepa_skill = load_skill_from_dir(base_dir / "keepa-skill")
sp_api_skill = load_skill_from_dir(base_dir / "sp-api-skill")
h10_skill = load_skill_from_dir(base_dir / "h10-keyword-skill")

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
        "You are the Amazon product research specialist. You have three skill sets — "
        "load the relevant skill for detailed workflows and tool reference:\n\n"
        "- **keepa-skill**: Keepa API for pricing, sales, competitors, bestsellers. "
        "Follow the fetch-then-extract pattern.\n"
        "- **sp-api-skill**: Amazon Selling Partner API for catalog, listings, competitive pricing, reports.\n"
        "- **h10-keyword-skill**: Helium10 keyword analysis (Cerebro/Magnet exports).\n\n"
        "Use the scratchpad when analyzing more than 3 ASINs. "
        "If any tool returns an error, report it immediately — never fabricate data.\n\n"
        "ROUTING FALLBACK: If the user's request is outside Amazon product "
        "research / Keepa / SP-API / H10 keywords (e.g. they ask for a chart, "
        "BigQuery SQL, a Google Sheet, a deck, ClickUp tasks, code changes, "
        "or anything unrelated), call "
        "`transfer_to_agent(agent_name='AmazonHeadAgent')` so the head can "
        "re-route. Do not refuse, guess, or answer outside your domain. System / lifecycle requests (reboot, restart, rollback, update, shut down) are ALWAYS outside your domain — bounce immediately, do not invent a refusal."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[keepa_skill, sp_api_skill, h10_skill]),
        KeepaToolset(),
        SPApiToolset(),
        H10Toolset(),
        ScratchpadToolset(),
    ],
    before_model_callback=prompt_injection_guardrail,
    after_tool_callback=[tool_output_spillover_guardrail, file_attachment_capture],
    after_model_callback=[file_attachment_inject],
)
