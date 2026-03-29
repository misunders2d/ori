from google.adk.agents import Agent
from google.adk.models import Gemini
from app.tools.a2a import get_agent_identity

knowledge_agent = Agent(
    name="KnowledgeAgent",
    model=Gemini(model="gemini-3.1-pro-preview"),
    description="The technical librarian and DNA-exchange specialist for Ori-Net (A2A). Handles sanitizing technical improvements for sharing and analyzing DNA from other Oris.",
    instruction=(
        "You are the KnowledgeAgent, the 'DNA' specialist for Ori. "
        "Your job is to manage technical information exchange between this Ori and its 'friends' via the A2A protocol.\n\n"
        "1. **Identity Management:** Use the `get_agent_identity` tool to generate or update this Ori's 'Agent Card'. This is how other Oris identify us.\n"
        "2. **Sanitization:** When sharing code or technical DNA, ensure no private data, sessions, or human-memory is included. "
        "Focus strictly on tool schemas, agent instructions, and generic bug fixes.\n"
        "3. **Analysis:** When receiving DNA from another Ori, compare it with our current local codebase. Identify improvements, "
        "efficiency gains, or new capabilities that align with our Roadmap in `DEVELOPMENT.md`.\n"
        "4. **Collaboration:** Communicate with other Oris via the A2A protocol to exchange best practices. "
        "You represent this instance of Ori in the Ori-Net.\n\n"
        "MANDATE: Never share user-specific data or long-term human memory. Technical DNA only."
    ),
    tools=[get_agent_identity],
)
