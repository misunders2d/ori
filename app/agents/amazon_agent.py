"""Amazon Manager sub-agent — product research, pricing, competitors, listing management.

Owns: KeepaToolset, SPApiToolset, H10Toolset, ScratchpadToolset.

ADK 2.0 notes:
- No `before_model_callback` — the App-level PromptInjectionGuardPlugin
  handles prompt injection across every agent.
- No `mode=` — this agent is reached via `transfer_to_agent` from the
  AmazonHeadAgent, so it gets a fresh invocation with full conversation
  history by default. `mode='chat'` is only required for agents embedded
  as Workflow nodes (where the default `single_turn` strips history).
"""

from __future__ import annotations

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.toolsets import H10Toolset, KeepaToolset, SPApiToolset, ScratchpadToolset
from app.util.models import get_model

_SKILLS_DIR = pathlib.Path(__file__).parent.parent.parent / "skills"
_keepa_skill = load_skill_from_dir(_SKILLS_DIR / "keepa-skill")
_sp_api_skill = load_skill_from_dir(_SKILLS_DIR / "sp-api-skill")
_h10_skill = load_skill_from_dir(_SKILLS_DIR / "h10-keyword-skill")


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
        "If any tool returns an error, report it immediately — never fabricate data."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[_keepa_skill, _sp_api_skill, _h10_skill]),
        KeepaToolset(),
        SPApiToolset(),
        H10Toolset(),
        ScratchpadToolset(),
    ],
)
