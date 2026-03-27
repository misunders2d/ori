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

## 🛠 Hatching Your Ori

Ori utilizes a hardened Docker deployment bound to a standalone internal user to keep it safely contained.

### 1. Incubation Setup
Create a `./data/.env` file to provide the initial nutrients:
```env
GOOGLE_API_KEY=your_key
ADMIN_PASSCODE=secret_bootstrap_code
TELEGRAM_BOT_TOKEN=your_token
GITHUB_TOKEN=for_self_evolution
```
*(Note: You can easily grab a free-tier `GOOGLE_API_KEY` by signing in to [Google AI Studio](https://aistudio.google.com/app/apikey). It is exclusively required to power the semantic anti-prompt-injection guardrail. The main autonomous agent itself is entirely model/provider-agnostic — simply ask Ori to switch to OpenAI, Anthropic, or any other provider you prefer!)*

### 2. Birth the Daemon
Run the automation launcher to spin up the container and the Host Supervisor:
```bash
./start.sh
```

### 3. Watching it Grow
When the `DeveloperAgent` triggers an evolution, a `.update_trigger` is injected into `/data/`. The `deploy.sh` script—acting as the "Host Supervisor"—intercepts this, safely shuts down the SQLite buffers, rebuilds the image, and notifies you via messenger that Ori has successfully evolved.

---
_"Don't just code your tools. Raise them."_
