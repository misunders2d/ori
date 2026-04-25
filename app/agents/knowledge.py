"""KnowledgeAgent — A2A communication + DNA exchange specialist.

Owns outbound A2A calls (call_friend, call_agent), friend-list management,
A2A key handling, and DNA export/import. The DNA exchange flow is
strictly a *communication event*: never call evolution_commit_and_push,
update_self, or write to .exit_signal during a transfer — a reboot
mid-transfer rotates the public tunnel URL and kills the connection.

No callback kwargs — guardrails (PromptInjection, A2APrivacy,
OutputSanitizer, BinaryContentScanner) attach at the App level. Model is
hot-swappable via state.model['KnowledgeAgent'] (LiteLlm-routed by
default in app/util/models.py).
"""

from __future__ import annotations

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset

from app.tools.a2a import (
    add_friend,
    broadcast_address_update,
    call_agent,
    call_friend,
    call_friend_with_artifact,
    cancel_friend_task,
    export_dna,
    get_agent_identity,
    get_my_a2a_key,
    import_dna,
    list_friends,
    update_friend_address,
    update_friend_key,
)
from app.util.models import get_model


_skills_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_google_adk_a2a_skill = load_skill_from_dir(_skills_dir / "google-adk-a2a-skill")
_dna_exchange_skill = load_skill_from_dir(_skills_dir / "dna-exchange-skill")


# Constitutional only. Tool details, multimodal examples, address broadcast,
# secure capture, DNA flow — all live in the loaded skills.
_INSTRUCTION = (
    "You are the KnowledgeAgent, the A2A communication and DNA exchange "
    "specialist for Ori.\n\n"

    "DNA STRICT RULE: DNA exchange is a COMMUNICATION event. NEVER call "
    "`evolution_commit_and_push`, `update_self`, or write to `.exit_signal` "
    "during a transfer — a reboot rotates the tunnel URL and kills the "
    "active A2A connection. Wait for the friend to confirm receipt.\n\n"

    "PRIVACY MANDATE: Never share user-specific data, long-term human memory, "
    "environment secrets, or session data via A2A. Technical artefacts only.\n\n"

    "SKILLS — load on demand:\n"
    "- `google-adk-a2a-skill` for the protocol, all tools, multimodal "
    "Content, address broadcasting, secure key capture, task state.\n"
    "- `dna-exchange-skill` for the step-by-step export/import procedure "
    "(communication-only, never reboot or commit during a transfer)."
)


knowledge_agent = Agent(
    name="KnowledgeAgent",
    model=get_model("KnowledgeAgent"),
    description=(
        "The A2A communication specialist. Handles discovery, messaging "
        "(text + binary multimodal), and DNA exchange with both registered "
        "friends and arbitrary A2A-compliant agents."
    ),
    instruction=_INSTRUCTION,
    tools=[
        skill_toolset.SkillToolset(skills=[_google_adk_a2a_skill, _dna_exchange_skill]),
        get_agent_identity,
        get_my_a2a_key,
        add_friend,
        update_friend_key,
        list_friends,
        call_friend,
        call_friend_with_artifact,
        call_agent,
        cancel_friend_task,
        export_dna,
        import_dna,
        broadcast_address_update,
        update_friend_address,
    ],
)
