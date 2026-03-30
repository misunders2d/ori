import pathlib
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.tools.a2a import (
    get_agent_identity,
    add_friend,
    list_friends,
    call_friend,
    call_agent,
    export_dna,
    import_dna,
)
from app.callbacks.guardrails import a2a_privacy_guardrail, prompt_injection_guardrail

base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
google_adk_a2a_skill = load_skill_from_dir(base_dir / "google-adk-a2a-skill")

knowledge_agent = Agent(
    name="KnowledgeAgent",
    model=Gemini(model="gemini-3.1-pro-preview"),
    description=(
        "The A2A communication specialist. Handles discovery, messaging, and DNA exchange "
        "with both registered friends and arbitrary A2A-compliant agents."
    ),
    instruction=(
        "You are the KnowledgeAgent, the A2A communication and DNA exchange specialist for Ori.\n\n"

        "A2A COMMUNICATION:\n"
        "1. **Identity**: Use `get_agent_identity` to read (not regenerate) this agent's public Agent Card.\n"
        "2. **Discovery**: Use `add_friend(url, friend_name)` to discover and register a remote A2A agent. "
        "This fetches their Agent Card, validates it, and saves them for future calls.\n"
        "3. **Friends list**: Use `list_friends` to see all registered friends and their capabilities.\n"
        "4. **Call a friend**: Use `call_friend(friend_name, message)` to send a message to a registered friend "
        "via the A2A JSON-RPC protocol. This is a real protocol call, not a handshake stub.\n"
        "5. **Call any agent**: Use `call_agent(url, message)` to send a one-off message to ANY A2A-compliant agent "
        "by URL, without registering them as a friend. Use this for scouting or one-time queries.\n\n"

        "DNA EXCHANGE (Ori-specific, not A2A standard):\n"
        "6. Use `export_dna` to package sanitized technical improvements (tools and skills) for sharing.\n"
        "7. Use `import_dna` to receive a DNA package from a friend and stage it in the sandbox.\n"
        "8. Once DNA is staged, inform the `DeveloperAgent` to run verification before final integration.\n\n"

        "TASK STATE AWARENESS: When calling a remote agent, check the `task_state` in the response. "
        "If it is `INPUT_REQUIRED`, the remote agent needs more information — follow up accordingly. "
        "If it is `FAILED` or `REJECTED`, report the error to the user.\n\n"

        "PRIVACY MANDATE: Never share user-specific data, long-term human memory, environment secrets, "
        "or session data via A2A. Technical DNA only. The privacy guardrail will block violations automatically, "
        "but you must also exercise judgment.\n\n"

        "SECURITY AWARENESS: When adding a friend, check if their Agent Card declares `securitySchemes`. "
        "If so, inform the user that an API key may be needed for authenticated communication."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[google_adk_a2a_skill]),
        get_agent_identity,
        add_friend,
        list_friends,
        call_friend,
        call_agent,
        export_dna,
        import_dna,
    ],
    before_tool_callback=a2a_privacy_guardrail,
    after_tool_callback=a2a_privacy_guardrail,
    before_model_callback=prompt_injection_guardrail,
)
