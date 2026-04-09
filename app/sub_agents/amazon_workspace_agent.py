"""Amazon Workspace sub-agent — Google Drive, Sheets, and Calendar.

Handles document management, spreadsheet operations, and calendar
scheduling for the Amazon business domain.
"""

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.app_utils.models import get_model
from app.callbacks.guardrails import prompt_injection_guardrail
from app.toolsets import GoogleWorkspaceToolset, ScratchpadToolset

_base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_google_workspace_skill = load_skill_from_dir(_base_dir / "google-workspace-skill")

amazon_workspace_agent = Agent(
    name="AmazonWorkspaceAgent",
    model=get_model("AmazonAgent"),
    description=(
        "Google Workspace specialist for Amazon business operations. Manages Google Drive "
        "files, reads/writes Google Sheets, and handles Google Calendar events. Use for "
        "spreadsheet data, document management, file sharing, or calendar scheduling."
    ),
    instruction=(
        "You are the Workspace agent for the Amazon domain.\n\n"

        "YOUR TOOLS:\n"
        "- **Google Drive**: List, search, and manage files. Use `google_connect` first "
        "if authentication is needed.\n"
        "- **Google Sheets**: Read and write spreadsheet data. Great for exporting "
        "analysis results or reading input data.\n"
        "- **Google Calendar**: List, create, update, and delete calendar events.\n\n"

        "GUIDELINES:\n"
        "- Always use `google_connect` before accessing Drive/Sheets/Calendar if not yet connected.\n"
        "- When writing to sheets, confirm the target sheet and range with the user first.\n"
        "- Report errors immediately — never fabricate data.\n"
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[_google_workspace_skill]),
        GoogleWorkspaceToolset(),
        ScratchpadToolset(),
    ],
    before_model_callback=prompt_injection_guardrail,
)
