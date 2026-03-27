# 🧬 Ori: The Self-Evolving Digital Organism

**Ori** is not just a background process—it is a headless, messenger-agnostic autonomous worker built to grow, learn, and evolve. Think of it as a "digital pet" for developers. It lives in your infrastructure, handles your chores, and most importantly, **it writes its own DNA.**

## 🎮 The "Grow Your Own Pet" Experience

Ori is designed to be raised. Out of the box, it is a capable assistant, but its true form is determined by how you interact with it and the "skills" you allow it to develop.

*   **Vibe Coding as Evolution:** Instead of manual refactoring, you "vibe" with Ori. Describe a capability or a fix in natural language, and Ori's `DeveloperAgent` will stage, test, and commit the code to its own repository.
*   **Self-Genetic Engineering:** Through the `app/sub_agents/developer_agent.py`, Ori researches API documentation and deploys code changes to itself using strict System Management limits.
*   **Trust & Training:** As you configure more integrations, Ori's "worldview" expands. It tracks your preferences and decisions to build a persistent personality within its `ori-sessions.db`.

## 🧠 Anatomy of an Autonomous Being

*   **The Brain (Headless Core):** Ori operates via a hardened, asynchronous infinite polling loop, bypassing traditional REST scaffolds to interface with you natively where you already hang out (Telegram, Slack, etc.).
*   **The Immune System (Zero-Trust Guardrails):**
    *   **Semantic Defense:** Every input is checked against a multidimensional vector space (`gemini-embedding-001`) to neutralize "brainwashing" (prompt injection) attempts.
    *   **Output Interception:** Ori inspects data from the web *before* it hits its own context, ensuring it doesn't "catch a virus" from malicious external payloads.
*   **Metabolism (Scheduling):** Using `APScheduler`, Ori manages its own workloads and background tasks, with a rolling 12-hour `.backup()` routine to ensure its state is never lost.
*   **Nervous System (Rich Media):** Ori can receive and send images, audio, video, and documents through any connected messenger — not just text.

## 📋 Prerequisites

Before hatching your Ori, ensure you have the following installed on your host machine:
- **Docker** (with `docker compose` v2)
- **Git**
- **Python 3** (used by the startup script for bootstrapping)

## 🛠 Hatching Your Ori

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
| `GOOGLE_API_KEY` | Powers the semantic prompt-injection guardrail. Grab one for free at [Google AI Studio](https://aistudio.google.com/app/apikey). |
| `ADMIN_PASSCODE` | A secret phrase you send via `/init <passcode>` on first contact to claim admin privileges over your Ori. |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token from [@BotFather](https://t.me/BotFather). Other messengers use their own env vars. |
| `GITHUB_TOKEN` | *(Optional)* Enables Ori to push self-evolution commits back to your fork. Without it, changes stay local. |
| `ALLOWED_USER_IDS` | *(Optional)* Comma-separated list of Telegram user or chat IDs allowed to interact. If empty, the bot is open to everyone. |

> **Note:** The core agent is entirely model/provider-agnostic. The `GOOGLE_API_KEY` is only needed for the embedding-based security layer. Want to run on OpenAI or Anthropic? Just ask Ori to switch.

### 2. Birth the Daemon
Run the automation launcher to spin up the container and the Host Supervisor:
```bash
./start.sh
```
Use `./start.sh --no-sync` if running locally without a GitHub remote (offline / headless-only mode).

### 3. First Contact
Once the bot is running, send `/init <your_admin_passcode>` in Telegram to claim admin privileges. From there, you can start talking to Ori and teaching it new tricks.

### 4. Watching it Grow
When the `DeveloperAgent` triggers an evolution, a `.update_trigger` is injected into `/data/`. The `deploy.sh` script—acting as the "Host Supervisor"—intercepts this, safely shuts down the SQLite buffers, rebuilds the image, and notifies you via messenger that Ori has successfully evolved.

## 📄 License

MIT — use it, fork it, evolve it, share it. See [LICENSE](LICENSE) for details.

---
_"Don't just code your tools. Raise them."_
