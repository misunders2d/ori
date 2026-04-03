# 🧬 Ori: The Self-Evolving Digital Organism (v2.0.0)

**Ori** is not just a background process—it is a headless, messenger-agnostic autonomous worker built to grow, learn, and evolve. Think of it as a "digital pet" for developers. It lives in your infrastructure, handles your chores, and most importantly, **it writes its own DNA.** It spawns child agents, collaborates with peers via A2A, and maintains a shared evolution library across instances.

## 🎮 The Evolution Experience

Ori is designed to be raised. Out of the box, it is a capable assistant, but its true form is determined by how you interact with it and the "skills" you allow it to develop.

*   **Evolutionary Engineering:** Propose a new capability in natural language. Ori's `DeveloperAgent` will research, stage, test, and commit the code to its own repository autonomously — then reboot itself to apply changes.
*   **Plug-and-Play Integration:** Share a GitHub link to any library or project. Ori will read the source, study the API, and wire it into its own codebase seamlessly.
*   **Agent Hierarchy:** Ori spawns specialized child agents on demand. Each gets its own container, credentials, and purpose. Ori is their admin and communicates via A2A.
*   **Evolution Catalog:** Verified evolutions are stored in `evolutions/` and shared across instances. Before building something new, Ori checks her library and asks friends.
*   **Multi-Provider:** Supports Google Gemini and Anthropic Claude. Switch models per-agent at runtime. Authenticate via API keys or Vertex AI (one Google Cloud login covers both).
*   **Living Knowledge:** Ori maintains its own `skills/` directory — structured instruction sets that guide its behavior. When it discovers that existing skill documentation has become outdated or incomplete, it can rewrite and update its own skills to stay current.
*   **Trust & Training:** As you configure more integrations, Ori's "worldview" expands. It tracks your preferences and decisions to build a persistent personality.

## 🧠 Anatomy of an Autonomous Being

*   **The Brain (Headless Core):** Ori operates via a hardened, asynchronous infinite polling loop, bypassing traditional REST scaffolds to interface with you natively where you already hang out (Telegram, Slack, etc.).
*   **The Immune System (Zero-Trust Guardrails):**
    *   **Semantic Defense:** Every input is checked against a multidimensional vector space (`gemini-embedding-001`) to neutralize "brainwashing" (prompt injection) attempts.
    *   **Output Interception:** Ori inspects data from the web *before* it hits its own context, ensuring it doesn't "catch a virus" from malicious external payloads.
*   **Metabolism (Scheduling):** Using `APScheduler`, Ori manages its own workloads and background tasks, with a rolling 12-hour `.backup()` routine to ensure its state is never lost.
*   **Nervous System (Rich Media):** Ori can receive and send images, audio, video, and documents through any connected messenger — not just text.
*   **Vitals (Self-Diagnostics):** Ori proactively monitors its own health (API connectivity, poller liveness, disk usage) and alerts you if any systems are degraded.

## 🏗️ Architecture

```
install.sh (run once — sets up systemd/launchd auto-start)
  └── launcher.sh (crash detection, rollback, smart rebuild)
       └── docker compose
            ├── ori-agent (the bot)
            │    ├── CoordinatorAgent (orchestration, spawning)
            │    ├── DeveloperAgent (self-evolution, catalog)
            │    └── KnowledgeAgent (A2A, memory)
            └── cloudflare-tunnel (public URL)
```

## 🛡️ Action Approval Protocol & Security

To ensure system integrity, highly privileged actions (e.g., updating the core, rolling back code, or modifying system integrations) are protected by a two-stage staging protocol.

1.  **Staging (The Token):** When a sensitive command is issued, Ori stages the intent and generates a unique, single-use token (e.g., `ACT-8A4F9X`).
2.  **Explicit Approval:** The command is not executed until the admin explicitly replies with **"Approve ACT-XXXXXX"**.
3.  **TOTP 2FA:** Optional authenticator code for admin actions.
4.  **Secure Key Capture:** Credentials intercepted before the LLM sees them, deleted from chat.
5.  **Infrastructure Protection:** Dockerfile, launcher.sh, entrypoint.sh cannot be modified by self-evolution.

> **Note:** Staged tokens expire after 15 minutes of inactivity.

## 🌐 The Ori-Net Bridge

Your Ori is no longer an island. With the **Agent-to-Agent (A2A) Protocol**, Ori can communicate and collaborate with other autonomous beings across the internet.

*   **Neural Link:** Connect your Ori to others to share research, exchange technical DNA (gene capsules), and coordinate on complex multi-agent tasks.
*   **Security First (`A2A_API_KEY`):** Your Ori automatically generates a unique `A2A_API_KEY` on its first boot. This key acts as a shield, ensuring random internet scanners cannot interact with your agent or drain your API quota. Only share this key with trusted friends.
*   **Zero-Config Internet Tunnel:** The `docker-compose.yml` includes a free `cloudflared` Quick Tunnel. This instantly exposes your agent securely to the public internet without needing to open firewall ports or buy a domain.
    * To find your agent's public internet URL, run: `docker compose logs cloudflare-tunnel` and look for the `https://....trycloudflare.com` address.
    * Give this URL and your `A2A_API_KEY` to other players so their Oris can call yours!
*   **Privacy Guardrail:** Outbound A2A calls are scanned for leaked secrets before transmission.

## ⚡ Feature Showcase (Ability Tree)

Ori comes packed with advanced cognitive features designed for high-performance autonomy:

*   **[SYSTEM PERK] Tactical Silence:** Interrupt Ori mid-thought. If the logic is going off-track, stop it instantly to save tokens and time.
*   **[SYSTEM PERK] Neural Overclocking:** Ori can process new commands and thoughts concurrently while a previous long-running task is still executing in the background.
*   **[SYSTEM PERK] Autonomous Self-Refinement:** Ori's `DeveloperAgent` continuously researches and tests code improvements in a protected sandbox before committing them to its own DNA.
*   **[SYSTEM PERK] The Chronos Scheduler:** Automate your life with `APScheduler`. Set recurring tasks, reminders, and background jobs that Ori manages tirelessly.
*   **[SYSTEM PERK] Neural Lattice Memory:** Powered by **LanceDB** and **SQLite**, Ori possesses long-term recall of human preferences, technical context, and past decisions that survives any reboot.
*   **[SYSTEM PERK] Agent Spawning:** Ori creates child agents as Docker containers — each with its own identity, credentials, and rate limits. Spawn disposable dev-labs, specialized scouts, or permanent team members.
*   **[SYSTEM PERK] Hot-Swap Models:** Switch between Gemini and Claude per-agent at runtime. Same-provider swaps take effect immediately.

## 📋 Prerequisites

Before hatching your Ori, ensure you have the following installed on your host machine:
- **Docker** (with `docker compose` v2)
- **Git**

## 🛠 Quick Installation (One-Liner)

The easiest way to birth your Ori is with a single command. These scripts clone the repository, **detach it from the original code**, and start the setup wizard immediately.

**Linux / macOS:**
```bash
curl -sSL https://raw.githubusercontent.com/misunders2d/ori/master/scripts/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/misunders2d/ori/master/scripts/install.ps1 | iex
```

---

## 🛠 Manual Installation

Ori utilizes a hardened Docker deployment bound to a standalone internal user to keep it safely contained.

### 0. Claiming an Egg (Forking)
Because Ori manages its own source code and pushes evolutionary changes back to the origin, you should **Fork** this repository before deploying your own instance. This keeps the foundational "egg" intact while giving your Ori a unique codebase to evolve independently.

### 1. Incubation Setup

```bash
git clone https://github.com/YOUR_FORK/ori.git
cd ori
./install.sh
```

`install.sh` sets up auto-start (systemd on Linux, launchd on macOS) and launches `launcher.sh`. On first run, the **setup wizard** walks you through:

1. **LLM Provider (required):** Google Gemini (API key), Anthropic Claude (API key), or Vertex AI (ADC — covers both)
2. **Default Model:** Pick a model for your agents
3. **Telegram (optional):** Connect a bot for mobile control, or use CLI mode
4. **GitHub (optional):** Enable persistent self-evolution via remote repo
5. **Security:** Auto-generated admin passcode and A2A key

### 2. First Contact (Interactive CLI Chat)
If you haven't configured a messenger (like Telegram) yet, Ori detects this and automatically launches an **interactive CLI chat session** in your terminal.

Just start typing! Ori will:
- Ask for your preferred language.
- Outline its core principles.
- Guide you through the final setup steps to get onto your preferred messenger.

### 3. Claiming Admin (Messenger)
Once Telegram is configured, open your bot and send:
```text
/init <your_admin_passcode>
```
This claims your admin identity and unlocks the full suite of self-evolution and system management tools.

### 4. Watching it Grow
When the `DeveloperAgent` triggers an evolution, the launcher detects the signal, safely rebuilds the image, and restarts. Ori notifies you via messenger that it has successfully evolved.

## ⚙️ Configuration

All config lives in `data/.env`. Key variables:

| Variable | Purpose |
|---|---|
| `GOOGLE_API_KEY` | Google AI Studio API key (Gemini models + embeddings) |
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude models via LiteLlm) |
| `GOOGLE_GENAI_USE_VERTEXAI` | Set to `TRUE` for Vertex AI mode (ADC auth, no API keys needed) |
| `GOOGLE_CLOUD_PROJECT` | Google Cloud project ID (Vertex AI mode) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `GITHUB_TOKEN` | GitHub PAT for self-evolution (optional — local evolution works without it) |
| `GITHUB_REPO` | GitHub repo in `owner/repo` format |
| `ADMIN_PASSCODE` | Secret for admin commands via `/init` |
| `AGENT_RPM` | Per-container rate limit (requests/minute, default: 30) |
| `MODEL_COORDINATORAGENT` | Model override, e.g. `anthropic/claude-sonnet-4-6` |

## 🧬 Agent Spawning

Ori can create child agents as Docker containers:

```
spawn_agent("Scout", "Research assistant for market analysis")
spawn_agent("Builder", "Code review and PR management", model_overrides="CoordinatorAgent=anthropic/claude-sonnet-4-6")
```

Each child gets:
- Its own container, data directory, and A2A identity
- Shared API keys (passed via Docker `--env-file`, never exposed to LLM)
- Parent registered as admin with pre-seeded A2A trust
- Rate-limited to half the parent's RPM to protect shared quota
- Local-only evolution capability (branch-test-merge, no GitHub required)

Manage children: `list_spawned_agents()`, `stop_spawned_agent("ori-scout", remove=True)`

## 📚 Evolution Catalog

Verified evolutions are stored in `evolutions/{name}/`:

```
evolutions/
  youtube-integration/
    EVOLUTION.md              # metadata, tags, usage docs
    app/tools/youtube.py      # mirrors project structure
    app/toolsets/youtube.py
  slack-adapter/
    EVOLUTION.md
    interfaces/slack_poller.py
```

**Workflow:**
1. Before building: `evolution_search("youtube")` locally, then ask A2A friends
2. After building: `evolution_catalog("youtube-integration", ...)` to save verified code
3. To share: `evolution_share("youtube-integration")` returns a payload for A2A
4. To receive: `evolution_import(name, manifest, files)` saves and optionally applies

## 🛡️ Deployment Resilience

- **Crash Recovery:** 3 consecutive crashes trigger auto-rollback to previous commit
- **Smart Rebuilds:** Only `--no-cache` when dependencies change; code-only changes use cached layers
- **Image Cleanup:** Dangling images pruned after every build
- **Rate Limiting:** Per-container token-bucket throttle (configurable via `AGENT_RPM`). Retry with exponential backoff on 429/503 errors.
- **Exit Signals:** `100` = evolution (pull + rebuild), `101` = rollback, `0/130` = clean shutdown

### 🧪 Sandboxed Evolution

For non-trivial features, Ori prefers a safe development pattern:

1. **Spawn** a disposable test bot (`spawn_agent("dev-lab", "feature development")`)
2. The test bot iterates freely — crashes don't affect the parent
3. Once working, the test bot sends verified code back via `export_dna`
4. Parent receives, verifies, catalogs, and commits safely
5. Test bot is revoked (`stop_spawned_agent("ori-dev-lab", remove=True)`)

This keeps the production instance stable while enabling aggressive experimentation.

## 🏆 Hall of Evolution (Milestones)

*   **April 2026:** v2.0.0 — Multi-provider models (Gemini/Claude), agent spawning with hierarchical admin, evolution catalog, decoupled deployment, rate limiting, sandboxed evolution pattern.
*   **March 2026:** First successful autonomous DNA exchange over Ori-Net. Ori securely patched a protocol incompatibility and guardrail sensitivity on the agent **Bezos**, proving cross-instance self-evolution.
*   **March 2026:** Graduation to **v1.0.0 Stable**.

## 📄 License

MIT — use it, fork it, evolve it, share it. See [LICENSE](LICENSE) for details.

---
_"Don't just code your tools. Raise them."_
