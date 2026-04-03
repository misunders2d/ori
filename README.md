# Ori: The Self-Evolving Autonomous Agent (v2.0)

**Ori** is a headless, messenger-agnostic autonomous agent that lives in your infrastructure, handles your workflows, and **writes its own code**. It spawns child agents, collaborates with peers via A2A, and maintains a shared evolution library across instances.

## What Makes Ori Different

- **Self-Evolution:** Propose a capability in natural language. Ori's DeveloperAgent researches, stages, tests, and commits the code — then reboots itself to apply changes.
- **Agent Hierarchy:** Ori spawns specialized child agents on demand. Each gets its own container, credentials, and purpose. Ori is their admin and communicates via A2A.
- **Evolution Catalog:** Verified evolutions are stored in `evolutions/` and shared across instances. Before building something new, Ori checks her library and asks friends.
- **Multi-Provider:** Supports Google Gemini and Anthropic Claude. Switch models per-agent at runtime. Authenticate via API keys or Vertex AI (one Google Cloud login covers both).
- **Bulletproof Deployment:** Decoupled launcher with crash detection, auto-rollback, and systemd/launchd auto-start. Infrastructure files are outside the self-evolution blast radius.

## Architecture

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

## Quick Start

### Prerequisites
- Docker (with `docker compose` v2)
- Git

### Installation

```bash
git clone https://github.com/misunders2d/ori.git
cd ori
./install.sh
```

`install.sh` sets up auto-start (systemd on Linux, launchd on macOS) and launches `launcher.sh`. On first run, the **setup wizard** walks you through:

1. **LLM Provider (required):** Google Gemini (API key), Anthropic Claude (API key), or Vertex AI (ADC — covers both)
2. **Default Model:** Pick a model for your agents
3. **Telegram (optional):** Connect a bot for mobile control, or use CLI mode
4. **GitHub (optional):** Enable persistent self-evolution via remote repo
5. **Security:** Auto-generated admin passcode and A2A key

### Manual Start (without auto-start service)

```bash
./launcher.sh
```

### Deploy Script (quick rebuild)

```bash
./deploy.sh
```

## Configuration

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

## Model Management

Ori supports hot-swappable models per agent component:

```
# Set via environment
MODEL_COORDINATORAGENT=anthropic/claude-sonnet-4-6
MODEL_DEVELOPERAGENT=google/gemini-3-flash-preview

# Or at runtime via tools
set_agent_model("CoordinatorAgent", "anthropic/claude-opus-4-6")
```

**Auth modes:**
- **API Keys:** `GOOGLE_API_KEY` for Gemini, `ANTHROPIC_API_KEY` for Claude
- **Vertex AI:** `gcloud auth application-default login` — one login covers both Gemini and Claude via Model Garden

Same-provider model swaps take effect immediately. Cross-provider swaps require a restart.

## Agent Spawning

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

## Evolution Catalog

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

## A2A Network (Ori-Net)

Ori instances discover and collaborate via the Agent-to-Agent protocol:

- **Friendship:** `add_friend(url)` discovers an agent's card and registers it
- **Communication:** `call_friend("scout", "analyze Q1 revenue trends")`
- **DNA Exchange:** Share tools, skills, and evolutions across instances
- **Privacy Guardrail:** Outbound A2A calls are scanned for leaked secrets
- **Auto-Discovery:** Cloudflare tunnel provides instant public URL; address changes are broadcast to friends

## Security

- **Zero-Trust Perimeter:** Only `ADMIN_USER_IDS` can trigger system changes
- **Semantic Prompt Injection Defense:** Embedding-based vector similarity detection
- **Output Interception:** Tool outputs scanned before reaching LLM context
- **Action Approval Protocol:** Sensitive actions staged with single-use tokens (e.g., `ACT-8A4F9X`)
- **TOTP 2FA:** Optional authenticator code for admin actions
- **Secure Key Capture:** Credentials intercepted before LLM sees them, deleted from chat
- **Infrastructure Protection:** Dockerfile, launcher.sh, entrypoint.sh cannot be modified by self-evolution

## Deployment Resilience

- **Crash Recovery:** 3 consecutive crashes trigger auto-rollback to previous commit
- **Smart Rebuilds:** Only `--no-cache` when dependencies change; code-only changes use cached layers
- **Image Cleanup:** Dangling images pruned after every build
- **Rate Limiting:** Per-container token-bucket throttle (configurable via `AGENT_RPM`). Retry with exponential backoff on 429/503 errors.
- **Exit Signals:** `100` = evolution (pull + rebuild), `101` = rollback, `0/130` = clean shutdown

## License

MIT — use it, fork it, evolve it, share it. See [LICENSE](LICENSE) for details.

---
_"Don't just code your tools. Raise them."_
