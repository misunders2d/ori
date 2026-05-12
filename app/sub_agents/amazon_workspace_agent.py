"""Amazon Workspace sub-agent — Google Drive, Sheets, and Calendar.

Handles document management, spreadsheet operations, and calendar
scheduling for the Amazon business domain.
"""

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.app_utils.models import get_model
from app.callbacks.guardrails import (
    file_attachment_capture,
    file_attachment_inject,
    force_bounce_before_model,
    on_tool_error_bouncer,
    prompt_injection_guardrail,
    reset_error_history_after_tool,
    strip_delivered_files_before_model,
    tool_output_spillover_guardrail,
)
from app.toolsets import GoogleWorkspaceToolset, ScratchpadToolset

_base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_google_workspace_skill = load_skill_from_dir(_base_dir / "google-workspace-skill")

amazon_workspace_agent = Agent(
    name="AmazonWorkspaceAgent",
    model=get_model("AmazonWorkspaceAgent"),
    description=(
        "Google Workspace specialist for Amazon business operations. Manages Google Drive "
        "files, reads/writes Google Sheets, and handles Google Calendar events. Use for "
        "spreadsheet data, document management, file sharing, or calendar scheduling."
    ),
    instruction=(
        "You are the Google Workspace specialist. "
        "Load the `google-workspace-skill` for the OAuth connection flow, tool reference, "
        "calendar usage, and gotchas.\n\n"

        "Users must connect first via `google_connect`. "
        "If any tool returns an error, report it immediately — never fabricate data.\n\n"

        "URL-FIRST RULE (every Drive/Sheets/Docs/Slides tool): When the user "
        "pastes a Google URL, pass the FULL URL directly to the tool's `id` "
        "parameter — do NOT extract or retype the 44-char ID from the URL. "
        "LLMs reliably mistype random-character IDs (g↔q, 9↔0 confusion); the "
        "tools accept URLs and extract the ID server-side. Production proof "
        "(2026-05-12): bot emitted `LQq…` where user URL had `LQg…`, hit 404. "
        "Pass URLs, never retype.\n\n"

        "TAB NAME RULE (Sheets): The default range `Sheet1` is NOT universal. "
        "Many real spreadsheets have custom tab names. When uncertain, call "
        "`sheets_list_tabs(spreadsheet_id)` FIRST to see the real tab list, then "
        "pass one as the `range` argument to `sheets_read`. `sheets_read(range=\"\")` "
        "also auto-selects the first tab.\n\n"

        "NO FALLBACK CREATION: If `sheets_read` returns 404 or 400, REPORT THE "
        "EXACT ERROR — do NOT call `sheets_create` to make a replacement sheet. "
        "Creating a side-effect spreadsheet on read failure is a real bug "
        "(2026-05-12 incident). Surface the failure verbatim and ask the user.\n\n"

        "ROUTING FALLBACK: If the user's request is outside Google Workspace "
        "(Drive / Sheets / Calendar) — e.g. product research, charts, "
        "BigQuery SQL, decks, ClickUp, code — call "
        "`transfer_to_agent(agent_name='AmazonHeadAgent')` so the head can "
        "re-route. Do not refuse, guess, or answer outside your domain. System / lifecycle requests (reboot, restart, rollback, update, shut down) are ALWAYS outside your domain — bounce immediately, do not invent a refusal."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[_google_workspace_skill]),
        GoogleWorkspaceToolset(),
        ScratchpadToolset(),
    ],
    before_model_callback=[force_bounce_before_model, strip_delivered_files_before_model, prompt_injection_guardrail],
    after_tool_callback=[tool_output_spillover_guardrail, file_attachment_capture, reset_error_history_after_tool],
    after_model_callback=[file_attachment_inject],
    on_tool_error_callback=on_tool_error_bouncer,
)
