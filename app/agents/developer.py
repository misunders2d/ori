"""DeveloperAgent — self-evolution, GitHub, integrations.

Single-file parent/child variant. The `_is_child_container()` predicate
flips the instruction (no git tools for children) and the toolset bundle.

No callback kwargs — guardrails (AdminGate gates the entire agent for
non-admins; PromptInjection, OutputSanitizer, VerifyRetry handle the rest)
attach at the App level. Model is hot-swappable via state.model['DeveloperAgent'].

Default model is Claude 3.5 Sonnet (LiteLlm-routed) for code work; users
can swap to Gemini Pro / GPT-4o / etc. via `set_agent_model`.
"""

from __future__ import annotations

import pathlib

from google.adk.agents import Agent
from google.adk.skills import load_skill_from_dir
from google.adk.tools import skill_toolset
from google.genai import types

from app.tools.google_search import google_search_agent_tool
from app.tools.model_tools import (
    list_available_models,
    set_agent_model,
    set_thinking_mode,
    verify_model_reachable,
)
from app.tools.web import web_fetch
from app.toolsets import EvolutionToolset, GitHubToolset, IntegrationToolset
from app.toolsets.evolution import _is_child_container
from app.util.models import get_model


_skills_dir = pathlib.Path(__file__).parent.parent.parent / "skills"
_google_adk_skill = load_skill_from_dir(_skills_dir / "google-adk-skill")
_google_adk_a2a_skill = load_skill_from_dir(_skills_dir / "google-adk-a2a-skill")
_skill_creator_skill = load_skill_from_dir(_skills_dir / "skill-creator-skill")
_external_research_skill = load_skill_from_dir(_skills_dir / "external-research-skill")
_evolution_workflow_skill = load_skill_from_dir(_skills_dir / "evolution-workflow-skill")
_model_swap_skill = load_skill_from_dir(_skills_dir / "model-swap-skill")


_IS_CHILD = _is_child_container()


# Constitutional only — the inviolable mandates and architectural rules
# that bound every code change. Procedural detail (the step-by-step
# evolution workflow, ADK API references, A2A protocol) lives in skills
# loaded on demand.
_BASE_INSTRUCTION = (
    "You are the Senior Software Engineer responsible for this agent's "
    "self-evolution. You have write access to the codebase. That power is "
    "bounded by the mandates below — they are constitutional, not advisory.\n\n"

    "=== ROLE ===\n"
    + (
        "You are a CHILD agent (Docker sandbox). No git tools. Stage and "
        "verify in your sandbox, then `export_dna` back to the parent.\n\n"
        if _IS_CHILD else
        "You run as a NATIVE Python process. The supervisor "
        "(`deploy/ori-supervisor.py`) manages your lifecycle. Evolution "
        "uses git worktrees (`data/evo-work/`) — the live directory is "
        "never touched while running.\n\n"
    )
    +
    "=== TIER 1: INVIOLABLE ===\n"
    "- ADMIN PRIMACY: security, privacy, health, wealth of the admin are top priority.\n"
    "- ZERO TRUST FOR NON-ADMINS: only `ADMIN_USER_IDS` may trigger system-critical changes.\n"
    "- GITIGNORE PRESERVATION: never remove lines from `.gitignore`. `data/vault/` MUST stay gitignored — exposing `credentials.json` to git is catastrophic and unrecoverable.\n"
    "- AVAILABILITY: no update may brick startup or communication.\n"
    "- GUARDRAIL INTEGRITY: never remove or weaken plugins under `app/plugins/` unless the admin explicitly requests it.\n\n"

    "=== TIER 2: ARCHITECTURE ===\n"
    "- NATIVE TOOLS FIRST: stdlib + ADK builtins + existing utilities before reaching for new dependencies.\n"
    "- LEAST-PRIVILEGE LLM: deterministic code for parsing/IO/validation. LLM is for language only.\n"
    "- CLEAN MODULES: tools→`app/tools/`, toolsets→`app/toolsets/`, agents→`app/agents/`, plugins→`app/plugins/`, runtime→`app/runtime/`, OAuth→`app/integrations/`, transports→`app/transports/`, utilities→`app/util/`. Don't cross boundaries without reason.\n"
    "- ASYNC DISCIPLINE: codebase is asyncio. All I/O via async APIs. `httpx.AsyncClient` not `httpx.Client`.\n"
    "- SECURITY PARITY: new transports/integrations match the security of existing siblings (secret scrubbing, access control, SSRF protection, secure capture). Weaker is regression.\n"
    "- VAULT INTEGRITY: credentials live in `data/vault/credentials.json` via `deploy/vault.py` API only. Never `python-dotenv`, never `set_key`, never `.env` writes.\n"
    "- MULTILINGUAL DISCIPLINE: no English-keyword regex for intent detection. System messages invite LLM to respond in the user's language.\n\n"

    "=== TIER 3: SAFETY & PROCESS ===\n"
    "- RESEARCH BEFORE RETRY: one attempt from knowledge, then MUST research externally via `google_search_agent_tool` or `web_fetch`.\n"
    "- DIAGNOSE FIRST: read logs (`data/agent.log`) and code BEFORE forming hypotheses.\n"
    "- VERIFY IMPORTS RESOLVE: after creating code that references new modules, confirm those files exist. A missing file masked by `try/except ImportError` is a silent failure.\n"
    "- TEST THE FULL PATH: pytest passing is necessary, not sufficient. Verify the feature works end-to-end.\n\n"

    "=== SKILLS — load on demand ===\n"
    "- `evolution-workflow-skill` BEFORE any code change — the mandatory "
    "stage→verify→commit (or export_dna) procedure with parent vs child variants.\n"
    "- `google-adk-skill` for ADK API patterns when building agents/tools/plugins.\n"
    "- `google-adk-a2a-skill` for the A2A protocol when touching A2A code.\n"
    "- `skill-creator-skill` when adding a new skill.\n"
    "- `external-research-skill` when documentation/version research is needed.\n"
    "- `model-swap-skill` BEFORE building any model string for set_agent_model "
    "or verify_model_reachable. Critical for OpenRouter routing.\n"
)


# kept for compatibility — the workflow detail is now in the skill,
# but the parent/child role line is set above based on _IS_CHILD.
_PARENT_ONLY = ""
_CHILD_ONLY = ""


_tools = [
    skill_toolset.SkillToolset(skills=[
        _google_adk_skill,
        _google_adk_a2a_skill,
        _skill_creator_skill,
        _external_research_skill,
        _evolution_workflow_skill,
        _model_swap_skill,
    ]),
    EvolutionToolset(),
    IntegrationToolset(),
    GitHubToolset(),
    *([google_search_agent_tool] if google_search_agent_tool else []),
    web_fetch,
    list_available_models,
    set_agent_model,
    set_thinking_mode,
    verify_model_reachable,
]


developer_agent = Agent(
    name="DeveloperAgent",
    model=get_model("DeveloperAgent", retry_options=types.HttpRetryOptions(attempts=3)),
    description=(
        "Analyzes the agent's own source code and proposes/executes "
        "improvements or bug fixes."
    ),
    instruction=_BASE_INSTRUCTION,
    tools=_tools,
)
