# Ori Daemon

**Ori** is a headless, messenger-agnostic autonomous worker daemon built on the `google.adk`. It serves as a persistent, localized background process that securely handles API integrations, cron-based scheduling, and self-evolving architectural tasks entirely via natural language interactions.

## Architecture & Tenets

*   **Headless Polling Core**: Ori natively bypasses traditional REST/FastAPI scaffolding. It relies on a hardened, asynchronous infinite polling loop to interface with the human operator natively.
*   **Total Messenger Independence**: No slash commands (`/think`, `/reset`, `/start`) are hardcoded. The daemon uses a generic `TransportAdapter` ABC, allowing it to natively scale across Telegram, Slack, WhatsApp, or local CLI seamlessly using a dynamically registered endpoint block.
*   **Database Concurrency**: The application formally isolates state buffers. `ori-sessions.db` tracks the human conversation layout, while `ori-scheduler.db` manages the heavy `APScheduler` cron workloads. This natively avoids concurrency locking. Both SQLite blocks feature a rolling 12-hour `.backup()` routine.
*   **Zero-Trust Guardrail Security**:
    *   **User Input Guardrail**: Incoming texts pass through a semantic vector check (`gemini-embedding-001`). If a prompt injection attempt matches the multidimensional anchor space, the LLM request is aborted.
    *   **Indirect Execution Guardrail**: The daemon also intercepts responses *after* a tool has been executed but before it hits the model context, completely neutralizing indirect prompt injections hidden inside external websites or web payloads.
*   **Self-Evolution**: Ori manages its own codebase (`app/sub_agents/developer_agent.py`). It reads, stages, tests (via local pytest sandboxing), and researches official API documentation logic securely using web search tools before deploying code changes to itself using strict `System Management` limits.

## Project Structure

```
ori/
├── app/                  # Core autonomous brain
│   ├── core/             # Central logical abstractions (Transport ABC, Agent Executors, Automated Backups)
│   ├── sub_agents/       # Granular specialized agents (Coordinator, Developer)
│   ├── app_utils/        # App utilities and configuration helpers
│   ├── callbacks/        # Runtime interceptors (Prompt Injection, Output Guardrails, Admin Auth)
│   ├── tools/            # Native ADK python tool definitions
│   └── scheduler_instance.py # APScheduler local jobstore map
├── interfaces/           # Messenger boundary implementations (e.g. telegram_poller)
├── scripts/              # Independent toolkits (Semantic Anchor Vectorizer, DB migrators)
├── skills/               # Developer instruction boundary specifications
├── data/                 # Ignored local environment configs and SQLite DB instances
├── deploy.sh             # Host-Level Update Supervisor
├── rollback.sh           # Host-Level Reversion Mechanism
├── docker-compose.yml    # Non-root daemon configuration 
└── run_bot.py            # Master Daemon Entrypoint
```

## Setup & Container Deployment

Ori utilizes a hardened Docker deployment bound to a standalone internal User `agentuser` to completely mitigate escape permissions.

### 1. Environment & Setup
Create a `./data/.env` file and define the following variables:
```env
GOOGLE_API_KEY=your_key
ADMIN_PASSCODE=secret_bootstrap_code
TELEGRAM_BOT_TOKEN=your_token
```

### 2. Standalone Automation Launcher
To cleanly mount volumes, configure prerequisites, fetch origin states, and spin up both the container and the asynchronous Host Supervisor bash scripts:
```bash
./start.sh
```
*(Use `./start.sh --no-sync` if testing entirely offline without a Github Remote)*.

### 3. Asymmetric Self-Updates
Because the Daemon operates headlessly, it does not use a `/health` endpoint container. When the `DeveloperAgent` naturally triggers the `evolution_commit_and_push` tools, a `.update_trigger` payload is injected into `/data/`. 

The `deploy.sh` script, running detached on the Host Linux Instance, intercepts this trigger, kills the Docker sequence safely allowing SQLite to shut down, checks out the most recent tested `origin shadow`, rebuilds the `ori-agent-daemon` image, confirms run states natively via `docker inspect`, automatically deletes dangling image chunks, and explicitly notifies the linked messenger transport asynchronously payloading the results!
