"""Amazon Manager sub-agent — product research, pricing, competitors, listing management.

Owns: Keepa toolset, Amazon SP API, Creatives (future).
"""

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.app_utils.models import get_model
from app.callbacks.guardrails import (
    amazon_ads_allowlist_guardrail,
    file_attachment_capture,
    file_attachment_inject,
    force_bounce_before_model,
    on_tool_error_bouncer,
    prompt_injection_guardrail,
    reset_error_history_after_tool,
    strip_delivered_files_before_model,
    surface_error_loudly_after_tool,
    tool_output_spillover_guardrail,
)
from app.toolsets import AmazonAdsToolset, KeepaToolset, ScratchpadToolset
from app.toolsets.sp_api import SPApiToolset
from app.toolsets.h10 import H10Toolset

base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
keepa_skill = load_skill_from_dir(base_dir / "keepa-skill")
sp_api_skill = load_skill_from_dir(base_dir / "sp-api-skill")
h10_skill = load_skill_from_dir(base_dir / "h10-keyword-skill")
ads_reporting_skill = load_skill_from_dir(base_dir / "ads-reporting")
ads_account_context_skill = load_skill_from_dir(base_dir / "account-context-identifiers")

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
        "- **h10-keyword-skill**: Helium10 keyword analysis (Cerebro/Magnet exports).\n"
        "- **ads-reporting**: Amazon Ads MCP — campaign/ad/target queries and "
        "async performance reports (`reporting-create_report` → poll "
        "`reporting-retrieve_report`). Read-heavy: NO campaign/budget/target "
        "writes — only account timezone, report create/delete, and DSP "
        "conversion-tracking product creation are permitted.\n"
        "- **account-context-identifiers**: pick the correct account scope "
        "(profileId vs advertiserAccountId vs managerAccountId) before any "
        "account-scoped Ads call. Read this BEFORE the first Ads tool.\n\n"
        "Amazon Ads tools are namespaced (`reporting-*`, `campaign_management-*`, "
        "`account_management-*`, …). Account scope is dynamic by default — pass "
        "account identifiers in the tool body; if scope is ambiguous (multiple "
        "accounts, or one account spanning many marketplaces), ASK the user "
        "before firing a report rather than guessing.\n\n"
        "Use the scratchpad when analyzing more than 3 ASINs. "
        "For Amazon Ads OUTPUT that needs charts / statistical analysis / a "
        "deck: write the raw report to a scratchpad named `ads_<kind>_<id>` "
        "and hand that pad name back via "
        "`transfer_to_agent(agent_name='AmazonHeadAgent')` so the head routes "
        "it to AmazonDataAnalystAgent — never paste large Ads CSVs into the "
        "conversation, never analyse them yourself. "
        "If any tool returns an error, report it immediately — never fabricate data.\n\n"
        "ROUTING FALLBACK: If the user's request is outside Amazon product "
        "research / Keepa / SP-API / H10 keywords (e.g. they ask for a chart, "
        "BigQuery SQL, a Google Sheet, a deck, ClickUp tasks, code changes, "
        "or anything unrelated), call "
        "`transfer_to_agent(agent_name='AmazonHeadAgent')` so the head can "
        "re-route. Do not refuse, guess, or answer outside your domain. System / lifecycle requests (reboot, restart, rollback, update, shut down) are ALWAYS outside your domain — bounce immediately, do not invent a refusal."
    ),
    tools=[
        skill_toolset.SkillToolset(
            skills=[
                keepa_skill,
                sp_api_skill,
                h10_skill,
                ads_reporting_skill,
                ads_account_context_skill,
            ]
        ),
        KeepaToolset(),
        SPApiToolset(),
        H10Toolset(),
        AmazonAdsToolset(),
        ScratchpadToolset(),
    ],
    before_model_callback=[force_bounce_before_model, strip_delivered_files_before_model, prompt_injection_guardrail],
    before_tool_callback=[amazon_ads_allowlist_guardrail],
    after_tool_callback=[tool_output_spillover_guardrail, file_attachment_capture, surface_error_loudly_after_tool, reset_error_history_after_tool],
    after_model_callback=[file_attachment_inject],
    on_tool_error_callback=on_tool_error_bouncer,
)
