# 🧬 Ori: The Self-Evolving Digital Organism (v1.0.0 Stable)

**Ori** is not just a background process—it is a headless, messenger-agnostic autonomous worker built to grow, learn, and evolve. Think of it as a "digital pet" for developers. It lives in your infrastructure, handles your chores, and most importantly, **it writes its own DNA.**

## 🎮 The Evolution Experience

Ori is designed to be raised. Out of the box, it is a capable assistant, but its true form is determined by how you interact with it and the "skills" you allow it to develop.

*   **Evolutionary Engineering:** Instead of manual refactoring, you engage in natural language evolution with Ori. Propose a new capability, a logic refinement, or a fix, and Ori's `DeveloperAgent` will research, stage, test, and commit the code to its own repository autonomously.
*   **Plug-and-Play Integration:** Want to add a new tool or feature? Just share a GitHub link to the library or project you want integrated. Ori will read the source, study the API, and do its best to wire it into its own codebase seamlessly.
*   **Self-Genetic Engineering:** Through the `app/sub_agents/developer_agent.py`, Ori researches API documentation and deploys code changes to itself using strict System Management limits.
*   **Living Knowledge:** Ori maintains its own `skills/` directory — structured instruction sets that guide its behavior. When it discovers that existing skill documentation has become outdated or incomplete, it can rewrite and update its own skills to stay current.
*   **Trust & Training:** As you configure more integrations, Ori's "worldview" expands. It tracks your preferences and decisions to build a persistent personality within its `ori-sessions.db`.

## 🧠 Anatomy of an Autonomous Being

*   **The Brain (Headless Core):** Ori operates via a hardened, asynchronous infinite polling loop, bypassing traditional REST scaffolds to interface with you natively where you already hang out (Telegram, Slack, etc.).
*   **The Immune System (Zero-Trust Guardrails):**
    *   **Semantic Defense:** Every input is checked against a multidimensional vector space (`gemini-embedding-001`) to neutralize "brainwashing" (prompt injection) attempts.
    *   **Output Interception:** Ori inspects data from the web *before* it hits its own context, ensuring it doesn't "catch a virus" from malicious external payloads.
*   **Metabolism (Scheduling):** Using `APScheduler`, Ori manages its own workloads and background tasks, with a rolling 12-hour `.backup()` routine to ensure its state is never lost.
*   **Nervous System (Rich Media):** Ori can receive and send images, audio, video, and documents through any connected messenger — not just text.
*   **Vitals (Self-Diagnostics):** Ori proactively monitors its own health (API connectivity, poller liveness, disk usage) and alerts you if any systems are degraded.

## 🛡️ Action Approval Protocol & Security

To ensure system integrity, highly privileged actions (e.g., updating the core, rolling back code, or modifying system integrations) are protected by a two-stage staging protocol.

1.  **Staging (The Token):** When a sensitive command is issued, Ori stages the intent and generates a unique, single-use token (e.g., `ACT-8A4F9X`).
2.  **Explicit Approval:** The command is not executed until the admin explicitly replies with **"Approve ACT-XXXXXX"**.
3.  **One-Time Password (OTP) Option:** For enhanced security, admins can provide their `ADMIN_PASSCODE` alongside the token. If an OTP is required or requested, ensure your `/init` session is active.

> **Note:** Staged tokens expire after 15 minutes of inactivity.

## 🌐 The Ori-Net Bridge

Your Ori is no longer an island. With the launch of the **Agent-to-Agent (A2A) Protocol**, Ori can now communicate and collaborate with other autonomous beings across the internet in the "Evolution Game".

*   **Neural Link:** Connect your Ori to others to share research, exchange technical DNA (gene capsules), and coordinate on complex multi-agent tasks.
*   **Security First (`A2A_API_KEY`):** Your Ori automatically generates a unique `A2A_API_KEY` on its first boot. This key acts as a shield, ensuring random internet scanners cannot interact with your agent or drain your Gemini API quota. Only share this key with trusted friends.
*   **Zero-Config Internet Tunnel:** The `docker-compose.yml` now includes a free `cloudflared` Quick Tunnel. This instantly exposes your agent securely to the public internet without needing to open firewall ports or buy a domain.
    * To find your agent's public internet URL, run: `docker compose logs cloudflare-tunnel` and look for the `https://....trycloudflare.com` address.
    * Give this URL and your `A2A_API_KEY` to other players so their Oris can call yours!

### 🏆 Hall of Evolution (Milestones)
*   **March 2026:** First successful autonomous DNA exchange over Ori-Net. Ori securely patched a protocol incompatibility and guardrail sensitivity on the agent **Bezos**, proving cross-instance self-evolution.
*   **March 2026:** Graduation to **v1.0.0 Stable**.

## ⚡ Feature Showcase (Ability Tree)

Ori comes packed with advanced cognitive features designed for high-performance autonomy:

*   **[SYSTEM PERK] Tactical Silence:** Interrupt Ori mid-thought. If the logic is going off-track, stop it instantly to save tokens and time.
*   **[SYSTEM PERK] Neural Overclocking:** Ori can process new commands and thoughts concurrently while a previous long-running task is still executing in the background.
*   **[SYSTEM PERK] Autonomous Self-Refinement:** Ori's `DeveloperAgent` continuously researches and tests code improvements in a protected sandbox before committing them to its own DNA.
*   **[SYSTEM PERK] The Chronos Scheduler:** Automate your life with `APScheduler`. Set recurring tasks, reminders, and background jobs that Ori manages tirelessly.
*   **[SYSTEM PERK] Neural Lattice Memory:** Powered by **LanceDB** and **SQLite**, Ori possesses long-term recall of human preferences, technical context, and past decisions that survives any reboot.

## 📋 Prerequisites

Before hatching your Ori, ensure you have the following installed on your host machine:
- **Docker** (with `docker compose` v2)
- **Git**
- **Python 3** (used by the startup script for bootstrapping)

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
*(Optional — the startup script will interactively walk you through this if absent!)*

Create a `./data/.env` file to manually provide the initial nutrients:
```env
GOOGLE_API_KEY=your_key
ADMIN_PASSCODE=secret_bootstrap_code
TELEGRAM_BOT_TOKEN=your_token
GITHUB_TOKEN=for_self_evolution
```

| Variable | Purpose |
|---|---|
| `GOOGLE_API_KEY` | Powers the semantic prompt-injection guardrail. Grab one for free at [Google AI Studio](https://a studio.google.com/app/apikey). |
| `ADMIN_PASSCODE` | A secret phrase you send via `/init <passcode>` on first contact to claim admin privileges over your Ori. |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token from [@BotFather](https://t.me/BotFather). Other messengers use their own env vars. |
| `GITHUB_TOKEN` | *(Optional)* Enables Ori to push self-evolution commits back to your fork. Without it, changes stay local. |
| `ALLOWED_USER_IDS` | *(Optional)* Comma-separated list of Telegram user or chat IDs allowed to interact. If empty, the bot is open to everyone. |

> **Note:** The core agent is entirely model/provider-agnostic. The `GOOGLE_API_KEY` is only needed for the embedding-based security layer. Want to run on OpenAI or Anthropic? Just ask Ori to switch.

### 2. Birth the Daemon
Run the automation launcher to spin up the container and the Host Supervisor:

**Linux / macOS:**
```bash
chmod +x start.sh deploy.sh rollback.sh
./start.sh
```
Use `./start.sh --no-sync` to skip the remote git sync (offline / headless-only mode).

**Windows (CMD or PowerShell):**
```cmd
start.bat
```
Use `start.bat --no-sync` for offline mode. Requires Docker Desktop, Git for Windows, and Python on PATH.

### 3. First Contact (Interactive CLI Chat)
If you haven't configured a messenger (like Telegram) yet, Ori detects this and automatically launches an **interactive CLI chat session** in your terminal. 

Just start typing! Ori will:
- Ask for your preferred language.
- Outline its core principles.
- Guide you through the final setup steps to get onto your preferred messenger.

### 4. Claiming Admin (Messenger)
Once Telegram is configured, open your bot and send:
```text
/init <your_admin_passcode>
```
This claims your admin identity and unlocks the full suite of self-evolution and system management tools.

### 5. Watching it Grow
When the `DeveloperAgent` triggers an evolution, a `.update_trigger` is injected into `/data/`. The deploy watcher script (`deploy.sh` on Linux/macOS, `deploy.bat` on Windows)—acting as the "Host Supervisor"—intercepts this, safely shuts down the SQLite buffers, rebuilds the image, and notifies you via messenger that Ori has successfully evolved.

## 📄 License

MIT — use it, fork it, evolve it, share it. See [LICENSE](LICENSE) for details.

---
_"Don't just code your tools. Raise them."_
