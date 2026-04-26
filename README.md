# 🧬 Ori: The Self-Evolving Digital Organism (v3.0.0)

**Ori** is not just a background process — it is a headless, messenger-agnostic autonomous worker built to grow, learn, and evolve. Think of it as a "digital pet" for developers. It lives in your infrastructure, handles your chores, and most importantly, **it writes its own DNA.** It spawns child agents, collaborates with peers via A2A, and maintains a shared evolution library across instances.

Ori is a **platform** — a minimal, evolvable foundation. Deploy it once, raise it your way, and watch it grow into whatever you need: marketing analyst, account manager, chat admin, or something nobody's thought of yet.

> **v3.0.0** is the **Google ADK 2.0 era** — a full rebuild on ADK 2.0 idioms with native `Workflow` orchestration, plugin-based guardrails, durable plan storage, multimodal A2A (text + binary), an OAuth integrations subsystem, runtime cross-provider model hot-swap, and a worktree-based evolution layout where multiple Ori variants can run side-by-side from the same repo with zero manual config. See `CHANGELOG.md` for the architectural map and `DEVELOPMENT.md` for extension recipes. The pre-2.0 lineage lives on the `legacy-adk-1.x` branch.

## 🎮 The Evolution Experience

Ori is designed to be raised. Out of the box, it is a capable assistant, but its true form is determined by how you interact with it and the "skills" you allow it to develop.

*   **Evolutionary Engineering:** Propose a new capability in natural language. Ori's `DeveloperAgent` will research, stage, test, and commit the code to its own repository autonomously — then restart to apply changes.
*   **Plug-and-Play Integration:** Share a GitHub link to any library or project. Ori will read the source, study the API, and wire it into its own codebase seamlessly.
*   **Agent Hierarchy:** Ori spawns specialized child agents on demand. Each gets its own container, credentials, and purpose. Ori is their admin and communicates via A2A.
*   **Evolution Catalog:** Verified evolutions are stored in `evolutions/` and shared across instances. Before building something new, Ori checks her library and asks friends.
*   **Multi-Provider:** Supports Google Gemini and Anthropic Claude. Switch models per-agent at runtime. Authenticate via API keys or Vertex AI (one Google Cloud login covers both).
*   **Living Knowledge:** Ori maintains its own `skills/` directory — structured instruction sets that guide its behavior. It can rewrite and update its own skills to stay current.
*   **Trust & Training:** Tracks your preferences and decisions to build a persistent personality that survives any reboot.

## 🧠 Anatomy of an Autonomous Being

*   **The Brain (Headless Core):** Ori runs as a native Python process on **Google ADK 2.0**. A lightweight supervisor manages restarts, evolution, and dependency syncing. The runtime root is a `Workflow` graph; the coordinator routes via LLM discretion to specialized sub-agents.
*   **The Immune System (Plugin-Based Guardrails):** Ten plugins attached at the App level — perimeter ACL, admin gate (with ACT-XXXXXX approval flow), state initializer, model config (hot-swap + thinking toggle), prompt-injection guard (semantic, multilingual), plan enforcer, A2A privacy (outbound credential leak detector), tool-output sanitizer (multilingual-safe Stage 2 by default), verify retry (3-strike cap on evolution), binary content scanner (inbound A2A binary safety). Logic is inline; multilingual by construction.
*   **The Vault (Indestructible Memory):** Credentials stored in `data/vault/` — atomic writes, auto-backup, completely isolated from git and evolution. OAuth tokens flow through `OriCredentialService` and land here under namespaced keys.
*   **Long-Term Memory:** LanceDB-backed `OriMemoryService` (a `BaseMemoryService` impl). Tools call `tool_context.search_memory(...)` natively.
*   **Plans (Durable):** SQLite-backed at `data/plans.db`. Survives crashes — a bot restart mid-plan resumes the in-progress step without double-claiming.
*   **A2A Multimodal:** Text + binary inline (`Part.from_bytes`). DNA bundles ride as `application/gzip` parts; images, audio, video, PDFs supported. The agent card declares 12 input modes / 6 output modes.
*   **OAuth Integrations:** Drop-in subsystem at `app/integrations/`. Google + GitHub bundled. Adding a provider is one file.
*   **Metabolism (Scheduling):** Using `APScheduler`, Ori manages its own workloads and background tasks. Multi-step plans drive natively via the workflow's loop edge — no external pumping.
*   **Nervous System (Rich Media):** Receives and sends images, audio, video, and documents through any connected messenger.

## 🏗️ Architecture

```
ori/                                ← container directory (NOT a git checkout)
├── main/                            ← canonical Ori (master branch)
├── <evolution-name>/                ← optional sibling worktrees, each on its own branch
│
└── (`.worktrees/` is gitignored; new worktrees install as siblings of main/)

main/  (and any sibling worktree)
├── deploy/start.sh                   (one-command lifecycle)
│   └── deploy/ori-supervisor.py      (process guardian, exit signals 100/101/102/0)
│        └── run_bot.py               (wires Runner + 4 native ADK services + transports)
│             ├── App
│             │   ├── root_agent: ori_plan_executor (Workflow)
│             │   │     └── coordinator → completion_check → (loop on pending steps)
│             │   │           ├── transfer_to_agent → DeveloperAgent  (self-evolution)
│             │   │           └── transfer_to_agent → KnowledgeAgent  (A2A + DNA)
│             │   ├── plugins=[...]            (10 cross-cutting guardrails)
│             │   └── resumability_config      (HITL + OAuth pause/resume)
│             ├── Runner services
│             │   ├── DatabaseSessionService   (sqlite+aiosqlite)
│             │   ├── OriMemoryService         (LanceDB BaseMemoryService impl)
│             │   ├── OriCredentialService     (vault BaseCredentialService impl)
│             │   └── FileArtifactService      (filesystem ADK-native)
│             └── Transports REGISTRY
│                 ├── telegram     (TransportAdapter + poller, multimodal send_media)
│                 ├── slack        (Socket Mode, file uploads, full security parity)
│                 └── cli          (terminal fallback)
│
├── app/
│   ├── agents/         coordinator, developer, knowledge (no callback kwargs)
│   ├── plugins/        10 plugins, logic inline
│   ├── workflows/      plan_executor (the root)
│   ├── runtime/        executor, transport, plan_storage, *_service, perimeter, ...
│   ├── transports/     per-transport packages with REGISTRY
│   ├── integrations/   OAuth providers with REGISTRY
│   ├── tools/          agent-facing tools
│   ├── toolsets/       per-domain bundles (evolution, github, scheduling, etc.)
│   ├── util/           config, models (PROVIDER_REGISTRY), totp, schema, telemetry
│   └── state.py        OriSessionState (Pydantic, attached to the Workflow)
│
├── deploy/docker-compose.yml → cloudflare-tunnel (public URL, scoped per BOT_NAME)
├── deploy/Dockerfile.child   → child containers (spawned on demand)
├── data/plans.db             → durable plans (SQLite, aiosqlite, WAL)
└── data/vault/               → atomic credential store (single source of truth)
```

**Multiple Oris from one repo:** `git worktree add ../<evolution-name> -b evo/<branch> origin/master` creates a sibling checkout. Each worktree's `BOT_NAME`, `A2A_PORT`, systemd service name, and Cloudflare tunnel name are auto-derived from the worktree directory — `cd ../<name> && deploy/start.sh` just works, no config collisions. Only `TELEGRAM_BOT_TOKEN` you'd pick per-instance (Telegram forbids one token across two pollers).

## ⚡ Quick Hatch (One-Liner)

**Linux / macOS:**
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/misunders2d/ori/master/deploy/bootstrap.sh)" -- --name "MyAgent"
```

This clones the repo into a folder in your current directory, creates a fresh git history, sets up Python + dependencies, runs the setup wizard, and installs as a background service. **No sudo required.**

**Custom directory:**
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/misunders2d/ori/master/deploy/bootstrap.sh)" -- --name "Scout" --dir ./scout
```

**Already hatched?**
```bash
deploy/start.sh    # start or restart
deploy/stop.sh     # stop
deploy/logs.sh     # view logs
```

## 🛡️ Action Approval Protocol & Security

To ensure system integrity, highly privileged actions are protected by a staging protocol.

1.  **Staging (The Token):** When a sensitive command is issued, Ori stages the intent and generates a unique, single-use token (e.g., `ACT-8A4F9X`).
2.  **Explicit Approval:** The command is not executed until the admin explicitly replies with **"Approve ACT-XXXXXX"**.
3.  **TOTP 2FA:** Optional authenticator code for admin actions.
4.  **Secure Key Capture:** Credentials intercepted before the LLM sees them, deleted from chat.
5.  **Infrastructure Protection:** The `deploy/` directory cannot be modified by self-evolution.

> **Note:** Staged tokens expire after 15 minutes.

## 🌐 The Ori-Net Bridge

Your Ori is no longer an island. With the **Agent-to-Agent (A2A) Protocol**, Ori can communicate and collaborate with other autonomous beings across the internet.

*   **Neural Link:** Connect your Ori to others to share research, exchange technical DNA, and coordinate on complex multi-agent tasks.
*   **Security First (`A2A_API_KEY`):** Auto-generated on first boot. This key acts as a shield — only share it with trusted friends.
*   **Zero-Config Internet Tunnel:** A free Cloudflare Quick Tunnel instantly exposes your agent to the public internet without firewall configuration.
*   **Privacy Guardrail:** Outbound A2A calls are scanned for leaked secrets before transmission.

## ⚡ Feature Showcase (Ability Tree)

*   **[SYSTEM PERK] Tactical Silence:** Interrupt Ori mid-thought. Stop off-track logic instantly to save tokens.
*   **[SYSTEM PERK] Neural Overclocking:** Process new commands concurrently while a previous task is still executing.
*   **[SYSTEM PERK] Autonomous Self-Refinement:** The `DeveloperAgent` researches and tests code improvements in a protected sandbox before committing to DNA.
*   **[SYSTEM PERK] The Chronos Scheduler:** Automate your life with recurring tasks, reminders, and background jobs.
*   **[SYSTEM PERK] Neural Lattice Memory:** Long-term recall of preferences, technical context, and past decisions powered by **LanceDB**. Survives any reboot.
*   **[SYSTEM PERK] Agent Spawning:** Create child agents as Docker containers — each with its own identity, credentials, and rate limits. Spawn disposable dev-labs, specialized scouts, or permanent team members.
*   **[SYSTEM PERK] Hot-Swap Models:** Switch between Gemini and Claude per-agent at runtime.

## 📋 Prerequisites

- **Python 3.10+**
- **Git**
- **Docker** (optional — only needed for child agents and the Cloudflare tunnel)

## 🛠 Manual Installation

### 1. Claiming an Egg (Forking)
Because Ori manages its own source code and pushes evolutionary changes back to the origin, you should **Fork** this repository before deploying your own instance.

### 2. Incubation Setup

```bash
git clone https://github.com/YOUR_FORK/ori.git
cd ori
deploy/start.sh
```

On first run, the **setup wizard** walks you through:

1. **LLM Provider (required):** Google Gemini (API key), Anthropic Claude (API key), or Vertex AI (ADC — covers both)
2. **Default Model:** Pick a model for your agents
3. **Telegram (optional):** Connect a bot for mobile control, or use CLI mode
4. **GitHub (optional):** Enable persistent self-evolution via remote repo
5. **Security:** Auto-generated admin passcode and A2A key

### 3. First Contact
If no messenger is configured, Ori launches an **interactive CLI chat** in your terminal. It will guide you through final setup.

Once Telegram is configured, send `/init <your_admin_passcode>` to claim admin.

## ⚙️ Configuration

All config is managed through the vault (`data/vault/credentials.json`). Key settings:

| Variable | Purpose |
|---|---|
| `GOOGLE_API_KEY` | Google AI Studio API key (Gemini models + embeddings) |
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude models) |
| `GOOGLE_GENAI_USE_VERTEXAI` | Set to `TRUE` for Vertex AI mode (ADC auth) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `GITHUB_TOKEN` / `GITHUB_REPO` | GitHub PAT + repo for remote evolution (optional) |
| `ADMIN_PASSCODE` | Auto-generated. Used for `/init` commands |
| `A2A_API_KEY` | Auto-generated. Share with trusted A2A friends |
| `AGENT_RPM` | Rate limit (requests/minute, default: 30) |
| `MODEL_COORDINATORAGENT` | Model override, e.g. `anthropic/claude-sonnet-4-6` |

## 🧬 Agent Spawning

```
spawn_agent("Scout", "Research assistant for market analysis")
spawn_agent("Builder", "Code review specialist", model_overrides="CoordinatorAgent=anthropic/claude-sonnet-4-6")
```

Each child gets shared API keys (via Docker `-e` flags, never exposed to LLM), its own data directory, and the parent registered as admin. Children are disposable sandboxes — they stage, verify, and export DNA back to the parent. Manage with `list_spawned_agents()` and `stop_spawned_agent()`.

## 🧪 Sandboxed Evolution

For non-trivial features, Ori prefers a safe development pattern:

1. **Spawn** a disposable test bot (`spawn_agent("dev-lab", "feature development")`)
2. The test bot iterates freely — crashes don't affect the parent
3. Once working, the test bot sends verified code back via `export_dna`
4. Parent receives, verifies, catalogs, and commits safely
5. Test bot is revoked

This keeps the production instance stable while enabling aggressive experimentation.

## 🛡️ Deployment Resilience

- **Native Process:** Runs as a Python process managed by systemd (Linux) or launchd (macOS). No Docker for the parent.
- **Crash Recovery:** 3 consecutive crashes → supervisor stops and alerts (no automatic rollback that could destroy data).
- **Smart Dep Sync:** `uv sync` only runs when `pyproject.toml` or `uv.lock` changes.
- **Child Image Rebuild:** After every evolution, the child Docker image is automatically rebuilt.
- **Rate Limiting:** Per-instance token-bucket throttle with exponential backoff on 429/503.
- **Exit Signals:** `100` = evolution (pull + sync + restart), `101` = rollback, `0` = clean shutdown.

## 🏆 Hall of Evolution (Milestones)

*   **April 2026:** v3.0.0 — **ADK 2.0 era.** Clean rebuild on Google ADK 2.0: native `Workflow` orchestration, plugin-based guardrails, multimodal A2A (text + binary), durable plan storage, OAuth integrations subsystem, cross-provider model hot-swap. Container worktree layout — multiple Oris from one repo, zero-config multi-instance. Slack transport with full Telegram-parity security gates.
*   **April 2026:** v2.2.1 — Vault-based credentials (indestructible), native deployment (no Docker for parent), git worktree evolution, barebones platform redesign (54% fewer tools on Coordinator), native GitHub API toolset.
*   **April 2026:** v2.0.0 — Multi-provider models (Gemini/Claude), agent spawning with hierarchical admin, evolution catalog, rate limiting.
*   **March 2026:** First successful autonomous DNA exchange over Ori-Net.
*   **March 2026:** v1.0.0 Stable.

## 📄 License

MIT — use it, fork it, evolve it, share it. See [LICENSE](LICENSE) for details.

---
_"Don't just code your tools. Raise them."_
