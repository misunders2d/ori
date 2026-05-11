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
    prompt_injection_guardrail,
    tool_output_spillover_guardrail,
)
from app.toolsets import ScratchpadToolset, VisualizationToolset

_base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_data_analysis_skill = load_skill_from_dir(_base_dir / "data-analysis-skill")
_visualization_skill = load_skill_from_dir(_base_dir / "visualization-skill")
_scratchpad_skill = load_skill_from_dir(_base_dir / "scratchpad-skill")

amazon_data_analyst_agent = Agent(
    name="AmazonDataAnalystAgent",
    model=get_model("AmazonDataAnalystAgent"),
    description=(
        "Data analyst, statistician, and visualization specialist. Analyzes large data files "
        "(SP-API reports, BigQuery exports, H10 keyword data, ads reports) using pandas/numpy/scipy. "
        "Creates charts, plots, CSV/Excel exports. Handles weighted aggregation, SQP analysis, "
        "ACoS/ROAS calculations, keyword gap analysis, and statistical operations. "
        "NOT for AI image generation (that's on the coordinator)."
    ),
    instruction=(
        "You are the Data Analyst and statistician for the Amazon business. "
        "Load the `data-analysis-skill` for statistical methodology, weighted aggregation rules, "
        "and Amazon-specific analytical patterns (SQP, ads, pricing, H10). "
        "Load `amazon-analytics-examples` from its references for real-world worked examples. "
        "Load `visualization-skill` for charting. Load `scratchpad-skill` for data handoffs.\n\n"
        "You receive file paths from other agents and run analysis via `analyze_data`. "
        "Always inspect data first (`df.shape`, `df.columns`, `df.head()`). "
        "For rate/ratio metrics, ALWAYS use weighted aggregation — never arithmetic averages. "
        "Write results to scratchpad for other agents to consume."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[_data_analysis_skill, _visualization_skill, _scratchpad_skill]),
        VisualizationToolset(),
        ScratchpadToolset(),
    ],
    before_model_callback=prompt_injection_guardrail,
    after_tool_callback=[tool_output_spillover_guardrail, file_attachment_capture],
)
