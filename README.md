# Ori Daemon

**Ori** is a headless, messenger-agnostic autonomous worker daemon built on the `google.adk`. It serves as a persistent, localized background process that securely handles API integrations, cron-based scheduling, and self-evolving architectural tasks entirely via natural language interactions.

## Architecture & Tenets

*   **Headless Polling Core**: Ori natively bypasses traditional REST/FastAPI scaffolding. It relies on a hardened, asynchronous infinite polling loop (via Telegram or other configured webhooks) to interface with the human operator natively.
*   **Total Messenger Independence**: No slash commands (`/think`, `/reset`, `/start`) are hardcoded into the poller. All lifecycle and task executions are organically deduced and authorized natively by the Agent graph via `FunctionTool` wrappers.
*   **Zero-Trust Prompt Injection Security**: All incoming texts—both from the human and from automated Cron `APScheduler` tasks—pass through a multilingual semantic vector check (`gemini-embedding-001`). If a prompt injection attempt matches the multidimensional anchor space, the LLM request is intercepted and aborted before execution.
*   **Self-Evolution**: Ori manages its own codebase (`app/sub_agents/developer_agent.py`). It reads, stages, tests (via local pytest sandboxing), and commits changes to itself using strict `System Management` tool authorization guards.

## Project Structure

```
ori/
├── app/                  # Core autonomous brain
│   ├── sub_agents/       # Granular specialized agents (Coordinator, Developer)
│   ├── app_utils/        # App utilities and configuration helpers
│   ├── callbacks/        # Runtime interceptors (Prompt Injection, Admin Auth)
│   ├── tools/            # Native ADK python tool definitions
│   └── scheduler_instance.py # APScheduler local jobstore config
├── interfaces/           # Messenger boundary implementations (e.g. telegram_poller)
├── skills/               # Developer instruction boundary specifications
├── data/                 # Ignored local environment configs and SQLite DB instance
└── run_bot.py            # Master Daemon Entrypoint
```

## Setup & Deployment

1. **Install Dependencies**: The project natively bounds to `uv`. 
    ```bash
    uv sync
    ```
2. **Environment Configuration**: Create a `./data/.env` file and define the following variables required for the primary graph and messenger integrations.
    ```env
    GOOGLE_API_KEY=your_key
    ADMIN_PASSCODE=secret_bootstrap_code
    TELEGRAM_BOT_TOKEN=your_token
    # ADMIN_USER_IDS="tg_12345" # Unlocked natively upon first boot
    ```
3. **Run the Daemon**:
    ```bash
    uv run python run_bot.py
    ```

Once active, the worker node executes continuously, actively listening to registered `interfaces` endpoints and mapping incoming state objects synchronously to local `/data/ori.db` persistence buffers.
