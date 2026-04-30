# Agent Evaluations

Trajectory-only regression tests for coordinator routing. Run before merging
changes to coordinator instructions or tool registration to catch regressions
introduced by self-evolution.

## Run

Requires `GOOGLE_API_KEY` (or other configured LLM provider) in env:

```bash
GOOGLE_API_KEY=... uv run --with "google-adk[eval]" adk eval ./app \
    tests/eval/evalsets/telegram_dm.evalset.json \
    --config_file_path tests/eval/eval_config.json
```

## What's covered

| Evalset | Cases | Asserts |
|---|---|---|
| `telegram_dm` | 3 | Coordinator routes "DM <name>" / "Message @<handle>" / "Tell <name> via telegram" requests to `telegram_send_dm` with the right `person` arg. Trajectory-only (`IN_ORDER`), so incidental tool calls (e.g. `get_current_time`) do not cause false failures. |

## What's NOT covered (intentionally)

- Response wording / quality (would need a judge model — expensive, brittle)
- Multi-turn conversations
- Error paths (`not_found` / `ambiguous`) — hard to fixture without seeding the roster
- Other agents (DeveloperAgent, KnowledgeAgent, AmazonHeadAgent)

Add cases narrowly when a specific regression bites.

## Notes

- `agents-cli eval run` doesn't recognize this project (expects a scaffolded
  layout); use `adk eval ./app ...` directly.
- The `--threshold 1.0 IN_ORDER` config means a case fails if the agent
  doesn't call `telegram_send_dm` at all, but tolerates extra tool calls
  before/after. Switch to `EXACT` if you want to assert no extras.
- Sub-agent transfers (`transfer_to_agent`) count as tool calls in the
  trajectory; if the coordinator delegates instead of calling
  `telegram_send_dm` directly, the case will fail.
