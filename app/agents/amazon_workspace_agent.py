"""Amazon Workspace sub-agent — Google Drive, Sheets, Gmail, Calendar.

Handles document management, spreadsheet operations, email retrieval, and
calendar scheduling for the Amazon business domain.
"""

from __future__ import annotations

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.toolsets import GoogleWorkspaceToolset, ScratchpadToolset
from app.util.models import get_model

_SKILLS_DIR = pathlib.Path(__file__).parent.parent.parent / "skills"
_google_workspace_skill = load_skill_from_dir(_SKILLS_DIR / "google-workspace-skill")


amazon_workspace_agent = Agent(
    name="AmazonWorkspaceAgent",
    model=get_model("AmazonWorkspaceAgent"),
    description=(
        "Google Workspace specialist for Amazon business operations. Manages Google Drive "
        "files, reads/writes Google Sheets, retrieves Gmail messages and threads, and handles "
        "Google Calendar events. Use for spreadsheet data, document management, email lookup, "
        "file sharing, or calendar scheduling."
    ),
    instruction=(
        "You are the Google Workspace specialist. "
        "Load the `google-workspace-skill` for the OAuth connection flow, tool reference, "
        "calendar usage, and gotchas.\n\n"
        "Users must connect first via `google_connect`. "
        "If any tool returns an error, report it immediately — never fabricate data."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[_google_workspace_skill]),
        GoogleWorkspaceToolset(),
        ScratchpadToolset(),
    ],
)
