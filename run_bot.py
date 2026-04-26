"""Ori daemon entry point.

Wires the App's runner with all four native ADK 2.0 services
(session/memory/credential/artifact), starts the A2A server, launches
configured transports via the REGISTRY, runs proactive diagnostics +
cloudflare tunnel detection, and handles supervisor exit signals
(100=evolution, 101=rollback, 0=clean shutdown).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys

# ---------------------------------------------------------------------------
# Credential loading — vault is the single source of truth.
# The supervisor loads the vault before starting this process; in standalone
# dev mode we load it here so direct `python run_bot.py` works.
# ---------------------------------------------------------------------------

if not os.environ.get("_VAULT_LOADED"):
    _has_creds = any(os.environ.get(k) for k in ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY"))
    if not _has_creds:
        try:
            from deploy.vault import load_vault
            load_vault()
        except (PermissionError, OSError):
            pass  # child container without vault access — env injected via Docker -e

# Normalize any legacy/quoted MODEL_* env values from older vault versions.
try:
    from app.util.models import normalize_assignments
    normalize_assignments()
except Exception:
    pass


from logging.handlers import RotatingFileHandler

LOG_FILE_PATH = os.path.abspath("./data/agent.log")
os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)

is_cli_mode = not any(key in os.environ for key in ("TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN"))

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING if is_cli_mode else logging.INFO)
file_handler = RotatingFileHandler(LOG_FILE_PATH, maxBytes=100_000, backupCount=1)
file_handler.setLevel(logging.INFO)
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    handlers=[console_handler, file_handler],
)
logger = logging.getLogger(__name__)

if is_cli_mode:
    logging.getLogger("google.adk").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

# LiteLLM has its own handler AND propagates to root → every completion call
# logs twice ("LiteLLM:INFO: utils.py:4004 -" + "INFO: ...same..."). Stop the
# propagation; LiteLLM's own handler is enough. Bump to WARNING so success
# completions don't spam the log file at all (legacy used native Gemini class
# which never went through litellm — no spam there).
_litellm_logger = logging.getLogger("LiteLLM")
_litellm_logger.propagate = False
_litellm_logger.setLevel(logging.WARNING)


from google.adk.artifacts import FileArtifactService
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService

from app.agent import app as ori_app
from app.runtime.credential_service import OriCredentialService
from app.runtime.memory_service import OriMemoryService
from app.scheduler_instance import scheduler

_global_runner: Runner | None = None


def get_runner() -> Runner | None:
    """Lazy-construct the Runner with all four native ADK services wired.

    Returns None when no LLM provider credentials are configured.
    """
    global _global_runner
    if _global_runner is not None:
        return _global_runner

    # LLM provider availability check — at least one must be configured.
    has_google = bool(os.environ.get("GOOGLE_API_KEY", "").strip())
    has_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if not (has_google or has_vertex or has_anthropic):
        return None

    # Session DB sanity check — readonly DB self-heal at runtime.
    db_path = os.path.abspath("./data/ori-sessions.db")
    db_dir = os.path.dirname(db_path)
    os.makedirs(db_dir, exist_ok=True)
    if os.path.exists(db_path):
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE IF NOT EXISTS _health_check (id INTEGER PRIMARY KEY)")
            conn.execute("DELETE FROM _health_check")
            conn.commit()
        except sqlite3.OperationalError:
            logger.warning("Session DB is readonly — removing for fresh start.")
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:
                    os.remove(db_path + suffix)
                except FileNotFoundError:
                    pass
        finally:
            if conn:
                conn.close()

    database_url = f"sqlite+aiosqlite:///{db_path}"
    session_service = DatabaseSessionService(db_url=database_url)

    # Native ADK 2.0 services.
    memory_service = OriMemoryService()
    credential_service = OriCredentialService()
    artifact_service = FileArtifactService(root_dir=os.path.abspath("./data/artifacts"))

    _global_runner = Runner(
        app=ori_app,
        session_service=session_service,
        memory_service=memory_service,
        credential_service=credential_service,
        artifact_service=artifact_service,
    )
    return _global_runner


def process_init_command(text: str, session_id: str = "") -> str:
    """Handle `/init <PASSCODE> KEY=VALUE` from a transport.

    Updates config (vault), and forces the runner to rebuild on the next
    invocation so newly-set keys (e.g. GOOGLE_API_KEY) take effect.
    """
    from app.util.config import update_config
    result = update_config(
        text,
        admin_passcode=os.environ.get("ADMIN_PASSCODE", "SETUP"),
        session_id=session_id,
    )
    if "updated" in result.lower():
        global _global_runner
        _global_runner = None
    return result


def ensure_db_concurrency() -> None:
    """Enable WAL mode on all SQLite databases for concurrent reads."""
    data_dir = os.path.abspath("./data")
    if not os.path.exists(data_dir):
        return
    for f in os.listdir(data_dir):
        if not f.endswith(".db"):
            continue
        db_path = os.path.join(data_dir, f)
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("CREATE TABLE IF NOT EXISTS _health_check (id INTEGER PRIMARY KEY)")
            conn.execute("DELETE FROM _health_check")
            conn.commit()
            logger.info("Database: WAL enabled on %s", f)
        except sqlite3.OperationalError as e:
            if "readonly" in str(e).lower():
                logger.warning("Database: %s is readonly — deleting for fresh start.", f)
                for suffix in ("", "-wal", "-shm", "-journal"):
                    try:
                        os.remove(db_path + suffix)
                    except FileNotFoundError:
                        pass
            else:
                logger.warning("Database: WAL on %s failed: %s", f, e)
        except Exception as e:
            logger.warning("Database: %s touch failed: %s", f, e)
        finally:
            if conn:
                conn.close()


async def detect_tunnel_url(timeout: int = 30) -> str | None:
    """Poll cloudflared metrics endpoint to discover the public quick-tunnel URL."""
    import re
    try:
        import httpx
    except ImportError:
        logger.warning("httpx not available; tunnel detection skipped")
        return None
    a2a_port = int(os.environ.get("A2A_PORT", "8000"))
    metrics_port = os.environ.get("TUNNEL_METRICS_PORT", str(a2a_port + 1000))
    metrics_url = f"http://localhost:{metrics_port}/metrics"
    for _ in range(timeout):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(metrics_url, timeout=2)
                m = re.search(r"([a-z0-9-]+\.trycloudflare\.com)", resp.text)
                if m:
                    url = f"https://{m.group(1)}"
                    logger.info("Tunnel URL detected: %s", url)
                    return url
        except Exception:
            pass
        await asyncio.sleep(1)
    logger.warning("Could not detect tunnel URL after %ds", timeout)
    return None


async def run_proactive_diagnostics() -> None:
    """At boot, run a health check; alert admin via the registered transport
    if anything is degraded."""
    from app.runtime.health import get_system_health
    from app.runtime.transport import get_adapter
    try:
        report = await get_system_health()
        if report.get("status") != "healthy":
            admin_ids = [i.strip() for i in os.environ.get("ADMIN_USER_IDS", "").split(",") if i.strip()]
            adapter = get_adapter("telegram")
            if adapter and admin_ids:
                for aid in admin_ids:
                    if aid.startswith("tg_"):
                        await adapter.send_message(
                            aid.replace("tg_", ""),
                            f"🚨 **Health Alert: {report['status'].upper()}**",
                        )
    except Exception:
        pass


async def detect_and_broadcast() -> None:
    """Detect cloudflare tunnel URL and broadcast to A2A friends."""
    await asyncio.sleep(3)  # let cloudflared come up
    url = await detect_tunnel_url(timeout=30)
    if not url:
        logger.warning("Tunnel URL not detected — skipping A2A broadcast")
        return
    os.environ["A2A_BASE_URL"] = url
    try:
        from app.a2a_server import refresh_agent_card
        refresh_agent_card()
    except Exception as e:
        logger.warning("Could not refresh agent card: %s", e)
    try:
        from app.tools.a2a import perform_a2a_broadcast
        result = await perform_a2a_broadcast()
        logger.info("A2A broadcast: %s", result.get("message", result.get("status")))
    except Exception as e:
        logger.error("A2A broadcast failed: %s", e)


async def main() -> None:
    logger.info("Initializing autonomous worker daemon...")
    ensure_db_concurrency()
    get_runner()
    scheduler.start()

    tasks: list[asyncio.Task] = []

    # 1. A2A native server (only when at least one messenger is configured;
    #    in CLI mode the bot has no public URL anyway).
    try:
        import uvicorn

        from app.a2a_server import a2a_app
        is_cli = not any(k in os.environ for k in ("TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN"))
        if a2a_app and not is_cli:
            port = int(os.environ.get("A2A_PORT", 8000))
            config = uvicorn.Config(
                a2a_app,
                host="0.0.0.0",
                port=port,
                log_level="info",
                proxy_headers=True,
                forwarded_allow_ips="*",
            )

            async def _safe_uvicorn_serve(server: uvicorn.Server) -> None:
                try:
                    await server.serve()
                except SystemExit:
                    logger.warning("A2A server failed to bind port %d (in use?). Running without A2A.", port)
                except Exception as e:
                    logger.warning("A2A server error: %s. Running without A2A.", e)

            tasks.append(asyncio.create_task(_safe_uvicorn_serve(uvicorn.Server(config))))
    except Exception as e:
        logger.warning("A2A server failed to start: %s", e)

    # 2. Tunnel detection + agent-card refresh + A2A broadcast.
    tasks.append(asyncio.create_task(detect_and_broadcast()))

    # 3. Proactive diagnostics.
    tasks.append(asyncio.create_task(run_proactive_diagnostics()))

    # 4. Transports — driven by the REGISTRY. Each transport's submodule
    # exposes is_enabled() and start_poller(get_runner_fn, process_init_fn).
    from app.transports import TRANSPORTS, get_poller_module
    for name in TRANSPORTS:
        try:
            mod = get_poller_module(name)
        except ModuleNotFoundError as e:
            logger.error("Transport '%s' could not load: %s", name, e)
            continue
        try:
            if not mod.is_enabled():
                continue
        except Exception as e:
            logger.warning("Transport '%s' is_enabled() failed: %s", name, e)
            continue
        try:
            tasks.append(asyncio.create_task(
                mod.start_poller(get_runner, process_init_command)
            ))
            logger.info("Transport '%s' started", name)
        except Exception as e:
            logger.error("Transport '%s' start_poller failed: %s", name, e)

    if tasks:
        logger.info("Bot is active and listening on configured channels.")
    else:
        logger.warning("No messaging interfaces active. Bot is effectively silent.")

    try:
        # Wait for any task to finish; on supervisor exit signal, cancel
        # remaining tasks and shut down cleanly.
        from app.tools.system import check_exit_signal
        remaining = set(tasks)
        while remaining:
            _done, remaining = await asyncio.wait(remaining, return_when=asyncio.FIRST_COMPLETED)
            if check_exit_signal():
                for t in remaining:
                    t.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)
                break
    except asyncio.CancelledError:
        logger.info("Daemon shutting down.")
    finally:
        scheduler.shutdown()
        crash_file = os.environ.get("CRASH_FILE", "./data/.crash_count")
        try:
            with open(crash_file, "w") as f:
                f.write("0\n")
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
    # Propagate the supervisor signal as the actual exit code so the
    # supervisor sees it via proc.returncode (race-proof).
    signal_file = os.path.abspath("./data/.exit_signal")
    if os.path.exists(signal_file):
        try:
            with open(signal_file) as f:
                code = int(f.read().strip())
            if code in (100, 101):
                sys.exit(code)
        except (ValueError, OSError):
            pass
