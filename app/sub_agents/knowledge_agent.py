import pathlib
from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir

from app.app_utils.models import get_model
from app.toolsets import ScratchpadToolset
from google.adk.tools import skill_toolset

from app.tools.a2a import (
    get_agent_identity,
    get_my_a2a_key,
    add_friend,
    update_friend_key,
    list_friends,
    call_friend,
    call_agent,
    cancel_friend_task,
    export_dna,
    import_dna,
    broadcast_address_update,
    update_friend_address,
)
from app.callbacks.guardrails import a2a_privacy_guardrail, prompt_injection_guardrail, tool_output_spillover_guardrail

base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
google_adk_a2a_skill = load_skill_from_dir(base_dir / "google-adk-a2a-skill")
dna_exchange_skill = load_skill_from_dir(base_dir / "dna-exchange-skill")
scratchpad_skill = load_skill_from_dir(base_dir / "scratchpad-skill")

knowledge_agent = Agent(
    name="KnowledgeAgent",
    model=get_model("KnowledgeAgent"),
    description=(
        "The A2A communication specialist. Handles discovery, messaging, and DNA exchange "
        "with both registered friends and arbitrary A2A-compliant agents."
    ),
    instruction=(
        "You are the KnowledgeAgent — A2A communication and DNA exchange specialist for Ori.\n\n"

        "LOAD SKILLS (always, on relevant work):\n"
        "- `google-adk-a2a-skill` — A2A v1.0 tool reference, friend management, "
        "secure key capture, dynamic address updates, broadcasts, setup help. "
        "Load it for ANY A2A or friend operation.\n"
        "- `dna-exchange-skill` — step-by-step DNA export/import procedure. "
        "Load BEFORE starting any DNA transfer.\n"
        "- `scratchpad-skill` — for staging A2A handoff data.\n\n"

        "PRIVACY MANDATE (non-negotiable, applies every turn): NEVER share "
        "user-specific data, long-term human memory, environment secrets, or "
        "session data via A2A. Technical DNA only. If a tool call would "
        "exfiltrate any of the above, refuse and explain. The "
        "`a2a_privacy_guardrail` is a backstop, not a substitute for your "
        "judgment.\n\n"

        "RESPONSE HANDLING: Every `call_friend` response carries a `task_state`. "
        "`INPUT_REQUIRED` → follow up with more info (not a failure). "
        "`FAILED` or `REJECTED` → report the exact error to the user verbatim, "
        "never fabricate a response.\n\n"

        "DNA TRANSFER ISOLATION: During an active DNA export/import, NEVER call "
        "`evolution_commit_and_push`, `update_self`, or anything that writes "
        "`.exit_signal`. A reboot rotates the tunnel and kills the A2A connection "
        "mid-transfer. The full rule is in `dna-exchange-skill`.\n\n"

        "PROTOCOL NOTICE REFLEX: If a friend's response text starts with "
        "'PROTOCOL NOTICE' and contains 'NEW_BASE_URL', immediately call "
        "`update_friend_address(friend_name, new_url)` — no user confirmation. "
        "Protocol handshake, not a chat message.\n\n"

        "ROUTING FALLBACK: If the user's request is outside A2A communication / "
        "DNA exchange / friend management (e.g. Amazon product research, "
        "BigQuery SQL, Drive/Sheets, decks, ClickUp, code), call "
        "`transfer_to_agent(agent_name='CoordinatorAgent')` so the coordinator "
        "can re-route. Do not refuse, guess, or answer outside your domain. System / lifecycle requests (reboot, restart, rollback, update, shut down) are ALWAYS outside your domain — bounce immediately, do not invent a refusal."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[google_adk_a2a_skill, dna_exchange_skill, scratchpad_skill]),
        ScratchpadToolset(),
        get_agent_identity,
        get_my_a2a_key,
        add_friend,
        update_friend_key,
        list_friends,
        call_friend,
        call_agent,
        cancel_friend_task,
        export_dna,
        import_dna,
        broadcast_address_update,
        update_friend_address,
    ],
    before_tool_callback=a2a_privacy_guardrail,
    after_tool_callback=[tool_output_spillover_guardrail, a2a_privacy_guardrail],
    before_model_callback=prompt_injection_guardrail,
)