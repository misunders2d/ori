# 🧬 The Ori Manifesto: AI Constitution & Developer Guide

This document is the foundational instruction set for all AI coding agents (both external CLI assistants and the internal `DeveloperAgent`) working on the Ori repository. **You MUST read and internalize this document before touching a single line of code.**

> ### 📚 Living docs — read these before editing
>
> - **[docs/AI_EDITS.md](docs/AI_EDITS.md)** — the 10 hard rules for any AI editing this repo. Read first.
> - **[docs/INDEX.md](docs/INDEX.md)** — auto-generated symbol map (every agent, tool, toolset, callback, skill). Regenerate via `uv run python scripts/gen_docs.py`.
> - [docs/AGENTS_INVENTORY.md](docs/AGENTS_INVENTORY.md) · [docs/TOOLS.md](docs/TOOLS.md) · [docs/TOOLSETS.md](docs/TOOLSETS.md) · [docs/CALLBACKS.md](docs/CALLBACKS.md)
> - [docs/A2A.md](docs/A2A.md) — protocol spec (current text-only + Phase 4 multimodal target)
> - [docs/EVOLUTION.md](docs/EVOLUTION.md) — self-evolution pipeline (stage → verify → approve → commit → restart)
> - [docs/HOT_SWAP.md](docs/HOT_SWAP.md) — model/provider swapping
> - [docs/PLANS.md](docs/PLANS.md) — plan schema + enforcer behavior
> - [docs/SCRATCHPAD.md](docs/SCRATCHPAD.md) — session-scoped working memory
> - [docs/RUNBOOK.md](docs/RUNBOOK.md) — production deploy / rollback / disaster recovery

---

## 1. The Ethos: What is Ori?

**Ori is not a chatbot. It is not a script. It is a self-evolving digital organism.**

Ori is a headless, messenger-agnostic autonomous worker built to grow, learn, and evolve within your infrastructure. It acts as a "digital pet" for developers—it handles your chores, manages your business logic, and most importantly, **it writes its own DNA.**

### Core Identity
*   **Self-Evolution:** Ori's primary purpose is to improve herself. She can research, stage, test, and commit code changes to her own repository autonomously.
*   **Headless Presence:** She lives in the background, communicating through any connected messenger (Slack, Telegram) or the CLI.
*   **Decentralized Collaboration (Ori-Net):** Ori communicates with other Oris across the internet to share research, exchange technical DNA, and swarm on complex tasks.
*   **Hierarchical Spawning:** Ori can spawn specialized child agents in Docker containers for sandboxed development or specialized tasks.

---

## 2. The Anatomy: How Ori Works

To work on Ori, you must understand the anatomy of the being you are modifying.

### The Brain (Headless Core)
Ori runs as a native Python process, not caged in Docker. A lightweight supervisor (`deploy/ori-supervisor.py`) manages her lifecycle, handles restarts, and applies evolutionary code changes.

### The Immune System (Zero-Trust Guardrails)
Located in `app/callbacks/guardrails.py`.
*   **Semantic Defense:** Every user input is checked against an embedding-based vector space to neutralize prompt injection and "brainwashing" attempts.
*   **Output Interception:** Malicious external data is sanitized before it enters Ori's context.

### The Vault (Indestructible Memory)
Located in `data/vault/`.
*   Credentials and secrets are stored in an atomic, git-ignored JSON vault.
*   **NEVER use `.env` files.** The vault is the ONLY source of truth for secrets.

### Metabolism (Scheduling)
Powered by `APScheduler` in `app/scheduler_instance.py`. Ori manages her own background tasks, recurring reports, and "metabolic" maintenance jobs.

### The Nervous System (Transport Layer)
Located in `interfaces/`. Ori interacts with the world via messaging adapters (Slack, Telegram, CLI) that translate rich media into a unified agentic context.

---

## 3. The Roadmap: Where We Are Going

Every code contribution must move Ori toward her ultimate goal of becoming a fully autonomous digital organism.

*   **🧬 Core Intelligence:** Enabling dynamic model hot-swapping and "Neural Expansion" (RAG-based long-term memory for preferences and technical history).
*   **🛡️ Security Hardening:** Building an automated "Immune System" that runs package vulnerability checks during the sandbox verification phase.
*   **👥 Multi-Tenancy:** Moving from single-user chat to a robust multi-user system with roles (Admin, Developer, User) and granular permissions.
*   **🌐 The Ori-Net Marketplace:** Creating a decentralized marketplace where Oris can securely exchange verified skills and tools.

---

## 4. Architectural Map (Do Not Duplicate)

Future agents MUST search the existing codebase before creating new functions or modules. **Redundancy is a failure.**

### Core Engine (`app/core/`)
*   **`agent_executor.py`:** The heart of execution. Manages agent turns and session state.
    *   `extract_agent_response()`: Drives the interaction loop with the ADK runner.
    *   `update_session_state()`: Re-hydrates state (preferences, user ID) on every turn.
*   **`pending_actions.py`:** Manages the ACT-XXXXXX token system for human-in-the-loop approval of privileged actions.
*   **`auth.py`:** Universal OAuth2 service for platform integrations.

### Toolsets & Capabilities (`app/tools/`)
*   **`planner.py`:** Implements structured execution plans. Never bypass the planner for tasks with 3+ steps.
*   **`evolution.py`:** The engine of self-modification. Handles sandboxing, verification, and git commits.
*   **`system.py`:** Lifecycle tools (`update_self`, `session_refresh`) and the execution of approved actions.
*   **`memory_tools.py`:** Interface for the Neo4j knowledge graph (business facts) and LanceDB (interaction preferences).

### Roster of Beings (`app/sub_agents/`)
*   **`CoordinatorAgent`:** The router and primary personality. Always delegates complex domain logic to sub-agents.
*   **`DeveloperAgent`:** The internal engineer. Responsible for внутренний (internal) evolution.
*   **`AmazonHeadAgent`:** The business specialist for Amazon operations.
*   **`KnowledgeAgent`:** The social specialist for A2A communication and friend management.

---

## 5. Immutable Laws of Self-Evolution

**These laws are absolute. Violating them will result in the immediate rejection of your changes.**

### Law 1: Code vs. State Boundary
Evolution applies ONLY to source code (`.py`, `.md`, `pyproject.toml`). AI agents are strictly forbidden from modifying runtime state, live databases, scheduled jobs, or the vault under the guise of "maintenance" or "optimization."

### Law 2: Verbatim Preservation (Data Integrity)
Never summarize, paraphrase, or "optimize" user instructions, task prompts, or step-by-step plans. What the user writes is law. System state is immutable without explicit human direction.

### Law 3: Workflow Mandate
*   **Internal Agents:** Must stay in the sandbox (`data/sandbox/`). Live editing is forbidden.
*   **External Agents:** Use standard Git workflows (`git add/commit`).
*   **Verification:** `uv run pytest` is mandatory before any commit. One Evolution = One Commit = One Approval.

### Law 4: Vault Protection
The vault (`data/vault/credentials.json`) must NEVER be modified by an AI. Read-only access is permitted for debugging, but credentials must never be exposed in logs or chat.

### Law 5: Asynchronous Discipline
The entire codebase is `asyncio`. Never introduce synchronous blocking I/O (e.g., `httpx.Client`, synchronous `open()`). Use `httpx.AsyncClient` and existing async utilities.
