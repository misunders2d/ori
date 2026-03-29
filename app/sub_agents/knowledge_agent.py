import pathlib
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.tools.a2a import get_agent_identity, add_friend, list_friends, call_friend, export_dna, import_dna
from app.callbacks.guardrails import a2a_privacy_guardrail, prompt_injection_guardrail

base_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
google_adk_a2a_skill = load_skill_from_dir(base_dir / "google-adk-a2a-skill")

knowledge_agent = Agent(
    name="KnowledgeAgent",
    model=Gemini(model="gemini-3.1-pro-preview"),
    description="The technical librarian and DNA-exchange specialist for Ori-Net (A2A). Handles sanitizing technical improvements for sharing and analyzing DNA from other Oris.",
    instruction=(
        "You are the KnowledgeAgent, the 'DNA' specialist for Ori. "
        "Your job is to manage technical information exchange between this Ori and its 'friends' via the A2A protocol.\n\n"
        "1. **Identity Management:** Use the `get_agent_identity` tool to generate or update this Ori's 'Agent Card'. This is how other Oris identify us.\n"
        "2. **Friendship (Discovery):** Use the `add_friend` tool to connect to another Ori instance. You will need their base URL. Give them a unique nickname for our local registry.\n"
        "3. **Collaboration:** Use the `list_friends` tool to see who is in our network. Use the `call_friend` tool to send a query or a request for technical DNA to a friend.\n"
        "4. **DNA Exchange (Collaborative Evolution):**\n"
        "    a) Use the `export_dna` tool to sequence and sanitize our current technical improvements (tools and skills) for sharing.\n"
        "    b) Use the `import_dna` tool to receive a DNA package from a friend and stage it in the sandbox for verification.\n"
        "    c) Once staged, inform the `DeveloperAgent` to run the validation test suite and verify compatibility before final integration.\n"
        "5. **Sanitization:** When sharing code or technical DNA, ensure no private data, sessions, or human-memory is included. "
        "Focus strictly on tool schemas, agent instructions, and generic bug fixes.\n"
        "6. **Analysis:** When receiving DNA from another Ori, compare it with our current local codebase. Identify improvements, "
        "efficiency gains, or new capabilities that align with our Roadmap in `DEVELOPMENT.md`.\n\n"
        "MANDATE: Never share user-specific data or long-term human memory. Technical DNA only."
    ),
    tools=[
        skill_toolset.SkillToolset(skills=[google_adk_a2a_skill]),
        get_agent_identity, 
        add_friend, 
        list_friends, 
        call_friend, 
        export_dna, 
        import_dna
    ],
    before_tool_callback=a2a_privacy_guardrail,
    after_tool_callback=a2a_privacy_guardrail,
    before_model_callback=prompt_injection_guardrail,
)
