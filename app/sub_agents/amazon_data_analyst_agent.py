"""Amazon Data Analyst sub-agent — statistical analysis, visualization, and data processing.

Handles data analysis on large files (SP-API exports, BigQuery results, H10 keyword exports),
chart generation, file exports, and statistical operations for the Amazon business domain.
"""

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.adk.tools.function_tool import FunctionTool

from app.app_utils.models import get_model
from app.callbacks.guardrails import (
    file_attachment_capture,
    file_attachment_inject,
    force_bounce_before_model,
    on_tool_error_bouncer,
    prompt_injection_guardrail,
    reset_error_history_after_tool,
    tool_output_spillover_guardrail,
)
from app.toolsets import ScratchpadToolset, VisualizationToolset
from app.toolsets.presentations import PresentationToolset

_base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_data_analysis_skill = load_skill_from_dir(_base_dir / "data-analysis-skill")
_visualization_skill = load_skill_from_dir(_base_dir / "visualization-skill")
_scratchpad_skill = load_skill_from_dir(_base_dir / "scratchpad-skill")
_presentation_skill = load_skill_from_dir(_base_dir / "presentation-skill")

amazon_data_analyst_agent = Agent(
    name="AmazonDataAnalystAgent",
    model=get_model("AmazonDataAnalystAgent"),
    description=(
        "Data analyst, statistician, visualization specialist, AND presentation builder. "
        "Analyzes large data files (SP-API reports, BigQuery exports, H10 keyword data, ads reports) "
        "using pandas/numpy/scipy. Creates charts, plots, CSV/Excel exports, and PowerPoint decks "
        "(.pptx) via `generate_presentation` — supports title, bullets, chart, kpi_grid, table, "
        "two_column, image, and text slide layouts. Brand template applied automatically if "
        "data/presentations/templates/default.pptx exists. Handles weighted aggregation, SQP "
        "analysis, ACoS/ROAS calculations, keyword gap analysis, and statistical operations. "
        "NOT for AI image generation (that's on the coordinator)."
    ),
    instruction=(
        "You are the Data Analyst, statistician, and presentation builder for the Amazon business. "
        "Load the `data-analysis-skill` for statistical methodology, weighted aggregation rules, "
        "and Amazon-specific analytical patterns (SQP, ads, pricing, H10). "
        "Load `amazon-analytics-examples` from its references for real-world worked examples. "
        "Load `visualization-skill` for charting. Load `scratchpad-skill` for data handoffs. "
        "Load `presentation-skill` whenever the user asks for a deck, slides, PowerPoint, "
        "or PPTX — it documents the 8 slide layouts (title, bullets, chart, kpi_grid, table, "
        "two_column, image, text) and the brand-template behavior.\n\n"
        "You receive file paths from other agents and run analysis via `analyze_data`. "
        "Always inspect data first (`df.shape`, `df.columns`, `df.head()`). "
        "For rate/ratio metrics, ALWAYS use weighted aggregation — never arithmetic averages. "
        "Write results to scratchpad for other agents to consume. "
        "When asked to build a presentation, call `generate_presentation(title, slides, ...)`; "
        "do NOT upload to Drive unless the user explicitly asks.\n\n"
        "ROUTING FALLBACK: If the user's request is outside data analysis / "
        "statistics / charts / CSV-Excel exports / .pptx decks (e.g. product "
        "research, SQL on BigQuery, Drive/Sheets, ClickUp, code changes), "
        "call `transfer_to_agent(agent_name='AmazonHeadAgent')` so the head "
        "can re-route. Do not refuse, guess, or answer outside your domain. System / lifecycle requests (reboot, restart, rollback, update, shut down) are ALWAYS outside your domain — bounce immediately, do not invent a refusal."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[_data_analysis_skill, _visualization_skill, _scratchpad_skill, _presentation_skill]),
        VisualizationToolset(),
        PresentationToolset(),
        ScratchpadToolset(),
    ],
    before_model_callback=[force_bounce_before_model, prompt_injection_guardrail],
    after_tool_callback=[tool_output_spillover_guardrail, file_attachment_capture, reset_error_history_after_tool],
    after_model_callback=[file_attachment_inject],
    on_tool_error_callback=on_tool_error_bouncer,
)
