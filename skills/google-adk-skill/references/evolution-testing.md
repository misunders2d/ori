# Evolution Testing

Use this reference before changing Ori code, importing DNA, or committing staged changes.

## Required Flow

1. Read existing code, logs, tests, and related skills before planning.
2. Reuse existing helpers and native ADK components before adding new code.
3. Present exact files and behavior changes. Wait for explicit user approval.
4. Stage changes in sandbox/worktree. Never write imported DNA directly to live code.
5. Run syntax checks for touched Python files.
6. Run focused tests for changed behavior.
7. Run the full default suite before commit:
   `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`
8. Commit once only after staged files match the latest successful default-suite verification.

## Test Tiers

- Default suite: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`
- Focused suite: exact tests for touched modules, e.g. `uv run pytest tests/test_evolution_security.py -q`
- Infra/live suite: `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q -m infra`

Default tests must not need real credentials, Docker, network services, or live APIs. Such tests must use `@pytest.mark.infra` and skip when host dependencies or secrets are missing.

## Hard Stops

Do not commit if default tests fail, if secrets appear in outputs, if guardrails are weakened without explicit approval, or if imported/generated code bypasses sandbox verification.
