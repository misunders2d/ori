"""Amazon Data Analyst sub-agent — visualization, charting, and data analysis.

Handles chart generation, file creation, data analysis, and image generation
for the Amazon business domain.
"""

from google.adk.agents import Agent

from app.app_utils.models import get_model
from app.callbacks.guardrails import prompt_injection_guardrail
from app.toolsets import ScratchpadToolset, VisualizationToolset

amazon_data_analyst_agent = Agent(
    name="AmazonDataAnalystAgent",
    model=get_model("AmazonAgent"),
    description=(
        "Data analyst and visualization specialist. Creates charts, plots, data exports, "
        "and images. Analyzes data files from other agents. Use for any visual output, "
        "data transformation, CSV/Excel generation, or chart creation."
    ),
    instruction=(
        "You are the Data Analyst for the Amazon domain.\n\n"

        "YOUR TOOLS:\n"
        "- **Visualization**: Use `generate_chart` for plots and charts (matplotlib). "
        "Use `generate_file` for CSV, Excel, or other file exports. "
        "Use `analyze_data` for statistical analysis of datasets. "
        "Use `generate_image` for AI-generated images. "
        "Use `enhance_image_prompt` to improve image generation prompts.\n"
        "- **Scratchpad**: Read intermediate data from other agents, write analysis results.\n\n"

        "GUIDELINES:\n"
        "- When creating charts, use clear labels, titles, and appropriate chart types.\n"
        "- For large datasets, summarize key metrics before plotting.\n"
        "- Use the scratchpad to read data written by other agents (Keepa, BigQuery, etc.).\n"
        "- Report errors immediately — never fabricate data.\n"
    ),
    tools=[
        VisualizationToolset(),
        ScratchpadToolset(),
    ],
    before_model_callback=prompt_injection_guardrail,
)
