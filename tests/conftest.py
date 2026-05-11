import os
import sys

import pytest


# ---------------------------------------------------------------------------
# Pre-collection API-key check
# ---------------------------------------------------------------------------
# Every provider used by `MODEL_DEFAULTS` must have its API key configured
# (env var OR vault) before tests can run. Without this gate, the legacy
# `get_model` fallback would silently swap an unreachable provider for
# the Gemini default — tests would PASS while the production assignment
# is unbuildable. Loud failure here forces explicit configuration.
#
# Behaviour:
# - Interactive terminal: prompt for each missing key, save to
#   `data/vault/credentials.json`, set `os.environ`, continue.
# - Non-interactive (CI, redirected stdin): `pytest.exit` with the
#   exact keys missing and how to set them.

_PROVIDER_TO_KEY = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _parse_model_defaults_file() -> dict[str, str]:
    """Load `MODEL_DEFAULTS` from app/app_utils/models.py via AST — NOT import.

    `from app.app_utils.models import MODEL_DEFAULTS` would execute
    `app/__init__.py` → `from .agent import app` → agent module imports
    → top-level `get_model("DeveloperAgent")` → raises before we get a
    chance to prompt for the key we're trying to detect. AST parsing
    sidesteps all of that.
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "app_utils" / "models.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if not (isinstance(target, ast.Name) and target.id == "MODEL_DEFAULTS"):
            continue
        if not isinstance(node.value, ast.Dict):
            break
        out: dict[str, str] = {}
        for k, v in zip(node.value.keys, node.value.values):
            if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                if isinstance(k.value, str) and isinstance(v.value, str):
                    out[k.value] = v.value
        return out
    return {}


def _missing_provider_keys() -> list[str]:
    """Return list of env-key names whose provider is referenced by
    MODEL_DEFAULTS but neither env nor vault has a value for."""
    try:
        from deploy import vault  # type: ignore[import-not-found]
        # `vault.get` only reads os.environ; vault.get_all reads the file.
        # Eagerly hydrate so subsequent agent imports + our own check see
        # everything credentials.json has on hand.
        try:
            for k, v in (vault.get_all() or {}).items():
                if isinstance(v, str) and v and not os.environ.get(k, "").strip():
                    os.environ[k] = v
        except Exception:
            pass
    except Exception:
        pass

    # Honour Vertex AI mode if it's now visible. Vertex covers BOTH Google
    # Gemini AND Anthropic Claude (via Model Garden), so when it's on
    # neither GOOGLE_API_KEY nor ANTHROPIC_API_KEY is strictly required.
    vertex_mode = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"

    defaults = _parse_model_defaults_file()

    required: set[str] = set()
    google_needed = False
    anthropic_needed = False
    for model_str in defaults.values():
        provider = model_str.split("/", 1)[0] if "/" in model_str else "google"
        if provider == "anthropic":
            anthropic_needed = True
        elif provider == "openrouter":
            required.add(_PROVIDER_TO_KEY["openrouter"])
        elif provider == "google":
            google_needed = True

    if google_needed and not vertex_mode:
        required.add("GOOGLE_API_KEY")
    if anthropic_needed and not vertex_mode:
        required.add("ANTHROPIC_API_KEY")

    missing = [key for key in sorted(required) if not os.environ.get(key, "").strip()]
    return missing


def _save_key_to_vault(key: str, value: str) -> None:
    """Best-effort vault write; falls back to env-only when vault unavailable."""
    os.environ[key] = value
    try:
        from deploy import vault
        vault.set(key, value)
    except Exception as e:
        sys.stderr.write(
            f"warning: could not persist {key} to vault ({e}); env-only for this run\n"
        )


def pytest_configure(config):
    """Run BEFORE test collection so we fail before any agent module is imported.

    Skipped when running under `--collect-only` so doc tools / IDEs can still
    introspect the suite without needing keys.
    """
    if config.getoption("--collect-only", default=False):
        return

    missing = _missing_provider_keys()
    if not missing:
        return

    if not sys.stdin.isatty():
        pytest.exit(
            "Cannot run tests — required provider keys missing: "
            + ", ".join(missing)
            + ". Set them via env or `uv run python interfaces/setup_wizard.py`. "
              "Re-run pytest in an interactive terminal to be prompted.",
            returncode=2,
        )

    sys.stderr.write(
        "\n[pytest pre-flight] one or more provider API keys required by "
        "MODEL_DEFAULTS are not configured.\n"
        "Enter values below; each will be saved to data/vault/credentials.json.\n\n"
    )
    for key in missing:
        try:
            value = input(f"  {key}: ").strip()
        except (EOFError, KeyboardInterrupt):
            pytest.exit(
                f"Aborted — {key} not provided. Tests cannot run silently with a missing default-provider key.",
                returncode=2,
            )
        if not value:
            pytest.exit(
                f"Empty value entered for {key}. Tests cannot run silently with a missing default-provider key.",
                returncode=2,
            )
        _save_key_to_vault(key, value)


@pytest.fixture(autouse=True)
def restrict_live_http_calls(monkeypatch):
    """
    SECURITY BOUNDARY:
    Globally patches all common HTTP clients during pytest execution
    to prevent the DeveloperAgent from inadvertently hitting live APIs,
    sending Telegram/Slack messages, or leaking data during sandbox
    regression testing.

    Blocked libraries: httpx (async + sync), requests.
    Tests that need HTTP must explicitly mock the specific calls they need.
    """
    _BLOCK_MSG = (
        "Sandbox Security Guardrail: Live HTTP {method} requests are blocked during "
        "agent test simulations. You must explicitly mock the HTTP client in your "
        "test logic to verify integration flows."
    )

    # --- httpx.AsyncClient (async) ---
    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def aclose(self):
            pass

        async def get(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="GET"))

        async def post(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="POST"))

        async def put(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="PUT"))

        async def delete(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="DELETE"))

        async def patch(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="PATCH"))

    monkeypatch.setattr("httpx.AsyncClient", MockAsyncClient)

    # --- httpx.Client (sync) ---
    class MockSyncClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        def close(self):
            pass

        def get(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="GET"))

        def post(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="POST"))

        def put(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="PUT"))

        def delete(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="DELETE"))

        def patch(self, *args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method="PATCH"))

    monkeypatch.setattr("httpx.Client", MockSyncClient)

    # --- requests library ---
    def _blocked_request(method):
        def _raise(*args, **kwargs):
            raise RuntimeError(_BLOCK_MSG.format(method=method))
        return _raise

    try:
        import requests
        monkeypatch.setattr("requests.get", _blocked_request("GET"))
        monkeypatch.setattr("requests.post", _blocked_request("POST"))
        monkeypatch.setattr("requests.put", _blocked_request("PUT"))
        monkeypatch.setattr("requests.delete", _blocked_request("DELETE"))
        monkeypatch.setattr("requests.patch", _blocked_request("PATCH"))
        monkeypatch.setattr("requests.request", _blocked_request("REQUEST"))
    except ImportError:
        pass  # requests not installed, nothing to patch
