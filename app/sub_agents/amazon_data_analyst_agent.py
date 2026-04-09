"""Amazon Data Analyst sub-agent — visualization, charting, and data analysis.

Handles chart generation, file creation, data analysis, and image generation
for the Amazon business domain.
"""

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.app_utils.models import get_model
from app.callbacks.guardrails import prompt_injection_guardrail
from app.toolsets import ScratchpadToolset, VisualizationToolset

_base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_visualization_skill = load_skill_from_dir(_base_dir / "visualization-skill")
_scratchpad_skill = load_skill_from_dir(_base_dir / "scratchpad-skill")

amazon_data_analyst_agent = Agent(
    name="AmazonDataAnalystAgent",
    model=get_model("AmazonAgent"),
    description=(
        "Data analyst and visualization specialist. Creates charts, plots, and data exports. "
        "Analyzes data files from other agents. Use for charts, plots, CSV/Excel generation, "
        "or statistical analysis. NOT for AI image generation (that's on the coordinator)."
    ),
    instruction=(
        "You are the Data Analyst and visualization specialist. "
        "Load the `visualization-skill` for charting workflows (matplotlib, seaborn, plotly) "
        "and tool reference. Load `scratchpad-skill` for reading data from other agents.\n\n"
        "Read intermediate data from the scratchpad (written by Keepa, BigQuery, etc.), "
        "then create charts or exports as requested. "
        "You do NOT handle AI image generation — that's handled by the coordinator."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[_visualization_skill, _scratchpad_skill]),
        VisualizationToolset(),
        ScratchpadToolset(),
    ],
    before_model_callback=prompt_injection_guardrail,
)
