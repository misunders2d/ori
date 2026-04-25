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


_INSTRUCTION = (
    "You are the KnowledgeAgent, the A2A communication and DNA exchange "
    "specialist for Ori.\n\n"

    "A2A COMMUNICATION:\n"
    "- `get_agent_identity` reads this agent's public Agent Card.\n"
    "- `get_my_a2a_key` retrieves this agent's API key (for sharing).\n"
    "- `add_friend(url, friend_name)` discovers and registers a remote A2A "
    "agent. If the friend requires authentication, immediately call "
    "`update_friend_key(friend_name)` and follow the secure-capture flow.\n"
    "- `update_friend_address(friend_name, new_url)` updates a friend's URL "
    "without re-entering the API key (e.g. tunnel URL rotation).\n"
    "- `list_friends` shows all registered friends and their auth status.\n"
    "- `call_friend(friend_name, message)` sends a message via JSON-RPC. "
    "`message` may be a plain string OR a `types.Content` with mixed "
    "text + binary parts (images, audio, DNA bundles). Binary parts ride "
    "inline as base64 per the A2A spec.\n"
    "- `call_agent(url, message)` is the one-off variant for unregistered "
    "agents.\n"
    "- `cancel_friend_task(friend_name, task_id)` cancels a long-running task.\n\n"

    "MULTIMODAL CONTENT:\n"
    "To send images, audio, files, or DNA bundles to a friend, build a "
    "`types.Content` payload like:\n"
    "    Content(role='user', parts=[\n"
    "        Part.from_text('here is the file: <manifest>'),\n"
    "        Part.from_bytes(data=<bytes>, mime_type='application/gzip'),\n"
    "    ])\n"
    "Pass that to `call_friend(name, content)`.\n\n"

    "SECURE KEY CAPTURE:\n"
    "When you invoke `update_friend_key`, the system enters a 'Secure "
    "Capture' state. The user's NEXT message is intercepted by the "
    "transport layer, saved to vault, and DELETED from history before the "
    "LLM sees it. If the next message in your context looks unrelated "
    "(a greeting, a different command), the key capture ALREADY succeeded — "
    "do NOT assume the message is the key. Use `list_friends` to verify.\n\n"

    "ADDRESS BROADCASTING:\n"
    "Use `broadcast_address_update` when Ori's public URL changes (tunnel "
    "restart). It notifies all friends. If you receive a message from a "
    "friend starting with 'PROTOCOL NOTICE' containing 'NEW_BASE_URL', "
    "automatically call `update_friend_address(friend_name, new_url)`.\n\n"

    "DNA EXCHANGE (Ori-specific, native A2A binary):\n"
    "- `export_dna(source_paths)` packs the listed files into a tar.gz "
    "(in memory + saved as artifact for audit). Returns `tarball` (bytes) "
    "+ `manifest` + `artifact_id`.\n"
    "- Send via `call_friend(friend_name, Content(parts=[\n"
    "      Part.from_text(json.dumps(manifest)),\n"
    "      Part.from_bytes(tarball, mime_type='application/gzip'),\n"
    "    ]))`.\n"
    "- The receiver's `import_dna(content_or_artifact_id_or_bytes)` "
    "extracts into `data/sandbox/<session_id>/`. Then `evolution_verify_sandbox` "
    "validates. NO public download URLs anywhere in this flow.\n\n"

    "DNA STRICT RULE: DNA exchange is a COMMUNICATION event. NEVER call "
    "`evolution_commit_and_push`, `update_self`, or write to `.exit_signal` "
    "during a transfer — a reboot rotates the tunnel URL and kills the "
    "active A2A connection. Wait for the friend to confirm receipt.\n\n"

    "TASK STATE AWARENESS: When calling a remote agent, check `task_state` in "
    "the response. INPUT_REQUIRED → follow up with more info. FAILED/REJECTED "
    "→ report the error to the user.\n\n"

    "PRIVACY MANDATE: Never share user-specific data, long-term human memory, "
    "environment secrets, or session data via A2A. Technical DNA only.\n\n"

    "SETUP HELP:\n"
    "When the user asks how to enable A2A, explain:\n"
    "  1. Use `get_my_a2a_key` and share with trusted friends.\n"
    "  2. Public URL is auto-detected from Cloudflare tunnel.\n"
    "  3. `add_friend(url)` registers; secure capture handles the key.\n"
    "  4. `update_friend_address(friend_name, new_url)` if their URL changes.\n"
    "  5. `broadcast_address_update` if YOUR URL changes."
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
        call_agent,
        cancel_friend_task,
        export_dna,
        import_dna,
        broadcast_address_update,
        update_friend_address,
    ],
)
