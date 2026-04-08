import asyncio
import logging
import os
import sqlite3
import sys

# ---------------------------------------------------------------------------
# Credential loading — all credentials come from the vault (data/vault/).
# The vault is loaded by the supervisor (deploy/ori-supervisor.py) BEFORE
# this process starts. In standalone mode (testing/dev), we load it here.
# No .env file, no python-dotenv, no config.json. Single source of truth.
# ---------------------------------------------------------------------------
if not os.environ.get("_VAULT_LOADED"):
    # Not started by the supervisor. Two cases:
    # 1. Spawned child container — credentials injected via Docker -e flags, already in os.environ
    # 2. Standalone dev mode — load from vault
    _has_creds = any(os.environ.get(k) for k in ["GOOGLE_API_KEY", "ANTHROPIC_API_KEY"])
    if not _has_creds:
        try:
            from deploy.vault import load_vault
            load_vault()
        except (PermissionError, OSError):
            pass  # Child container without vault access — credentials from -e flags

from logging.handlers import RotatingFileHandler
LOG_FILE_PATH = os.path.abspath("./data/agent.log")
os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)

is_cli_mode = not any(key in os.environ for key in ["TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN"])

# Send clean warnings to console in CLI mode, but keep full INFO in the log file
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING if is_cli_mode else logging.INFO)

file_handler = RotatingFileHandler(LOG_FILE_PATH, maxBytes=100_000, backupCount=1)
file_handler.setLevel(logging.INFO)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    handlers=[console_handler, file_handler]
)
logger = logging.getLogger(__name__)

if is_cli_mode:
    logging.getLogger("google.adk").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from app.agent import app as ori_app
from app.scheduler_instance import scheduler

_global_runner = None

def get_runner():
    global _global_runner
    if not _global_runner:
        # Check for any valid LLM provider (API key or Vertex AI)
        has_google = bool(os.environ.get("GOOGLE_API_KEY", "").strip())
        has_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
        has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
        if not (has_google or has_vertex or has_anthropic):
            return None
        db_path = os.path.abspath("./data/ori-sessions.db")
        # Verify the session DB directory is writable
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
        _global_runner = Runner(app=ori_app, session_service=session_service)
    return _global_runner

def process_init_command(text: str, session_id: str = "") -> str:
    from app.app_utils.config import update_config
    result = update_config(text, admin_passcode=os.environ.get("ADMIN_PASSCODE", "SETUP"), session_id=session_id)
    if "updated" in result.lower():
        global _global_runner
        _global_runner = None
    return result

def ensure_db_concurrency():
    """Enables SQLite Write-Ahead Logging (WAL) for better concurrency and fewer 'database is locked' errors."""
    data_dir = os.path.abspath("./data")
    if not os.path.exists(data_dir): return
    for f in os.listdir(data_dir):
        if f.endswith(".db"):
            db_path = os.path.join(data_dir, f)
            conn = None
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA journal_mode=WAL;")
                # Write-test: catch readonly databases early
                conn.execute("CREATE TABLE IF NOT EXISTS _health_check (id INTEGER PRIMARY KEY)")
                conn.execute("DELETE FROM _health_check")
                conn.commit()
                logger.info(f"Database: Concurrency optimized for {f} (WAL mode).")
            except sqlite3.OperationalError as e:
                if "readonly" in str(e).lower():
                    logger.warning(f"Database: {f} is readonly — deleting for fresh start.")
                    for suffix in ("", "-wal", "-shm", "-journal"):
                        try:
                            os.remove(db_path + suffix)
                        except FileNotFoundError:
                            pass
                else:
                    logger.warning(f"Database: Could not optimize {f}: {e}")
            except Exception as e:
                logger.warning(f"Database: Could not optimize {f}: {e}")
            finally:
                if conn:
                    conn.close()

async def detect_tunnel_url(timeout=30) -> str | None:
    """Poll cloudflared metrics endpoint to discover the quick tunnel URL."""
    import re
    try:
        import httpx
    except ImportError:
        logger.warning("httpx not available, cannot detect tunnel URL")
        return None

    a2a_port = int(os.environ.get("A2A_PORT", "8000"))
    metrics_port = os.environ.get("TUNNEL_METRICS_PORT", str(a2a_port + 1000))
    metrics_url = f"http://localhost:{metrics_port}/metrics"
    for attempt in range(timeout):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(metrics_url, timeout=2)
                match = re.search(r'([a-z0-9-]+\.trycloudflare\.com)', resp.text)
                if match:
                    url = f"https://{match.group(1)}"
                    logger.info("Tunnel URL detected: %s", url)
                    return url
        except Exception:
            pass
        await asyncio.sleep(1)
    logger.warning("Could not detect tunnel URL after %ds", timeout)
    return None


async def run_proactive_diagnostics():
    from app.core.health import get_system_health
    from app.core.transport import get_adapter
    try:
        report = await get_system_health()
        if report["status"] != "healthy":
            admin_ids = [i.strip() for i in os.environ.get("ADMIN_USER_IDS", "").split(",") if i.strip()]
            adapter = get_adapter("telegram")
            if adapter and admin_ids:
                for aid in admin_ids:
                    if aid.startswith("tg_"):
                        await adapter.send_message(aid.replace("tg_", ""), f"🚨 **Health Alert: {report['status'].upper()}**")
    except Exception: pass

async def main():
    logger.info("Initializing Autonomous Worker Daemon...")
    ensure_db_concurrency()
    runner = get_runner()
    scheduler.start()
    tasks = []
    
    # 1. A2A Native Server
    try:
        import uvicorn
        from app.a2a_server import a2a_app

        # Determine if we are in CLI mode (no messengers configured)
        is_cli = not any(key in os.environ for key in ["TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN"])

        # Only start Uvicorn if not in CLI mode to prevent TTY hijacking and config errors
        if a2a_app and not is_cli:
            port = int(os.environ.get("A2A_PORT", 8000))
            config = uvicorn.Config(
                a2a_app,
                host="0.0.0.0",
                port=port,
                log_level="info",
                proxy_headers=True,
                forwarded_allow_ips="*"
            )

            async def _safe_uvicorn_serve(server):
                """Wrap uvicorn.serve() to catch port-bind failures (SystemExit)."""
                try:
                    await server.serve()
                except SystemExit:
                    logger.warning("A2A server failed to bind port %d (in use?). Running without A2A.", port)
                except Exception as e:
                    logger.warning("A2A server error: %s. Running without A2A.", e)

            tasks.append(asyncio.create_task(_safe_uvicorn_serve(uvicorn.Server(config))))
    except Exception as e:
        logger.warning(f"A2A Server failed to start: {e}")

    # 2. Post-boot: warn admin if Google API key is missing (needed for security guardrails)
    async def _warn_missing_google_key():
        await asyncio.sleep(10)  # let Telegram poller start first
        google_key = os.environ.get("GOOGLE_API_KEY", "").strip()
        if google_key:
            return
        admin_ids = [i.strip() for i in os.environ.get("ADMIN_USER_IDS", "").split(",") if i.strip()]
        from app.core.transport import get_adapter
        adapter = get_adapter("telegram")
        if adapter and admin_ids:
            passcode_hint = os.environ.get("ADMIN_PASSCODE", "YOUR_PASSCODE")[:3] + "..."
            msg = (
                "**Security Notice:** No Google API key detected.\n\n"
                "The embedding-based prompt injection defense is disabled. "
                "To enable it, send:\n"
                f"`/init {passcode_hint} GOOGLE_API_KEY=your-key`\n\n"
                "Get a free key at: https://aistudio.google.com/app/apikey"
            )
            for aid in admin_ids:
                chat_id = aid.replace("tg_", "") if aid.startswith("tg_") else aid
                try:
                    await adapter.send_message(chat_id, msg)
                except Exception:
                    pass
    tasks.append(asyncio.create_task(_warn_missing_google_key()))

    # Automatic tunnel detection + A2A Broadcast
    async def detect_and_broadcast():
        # Give cloudflared a moment to establish the tunnel
        await asyncio.sleep(3)
        tunnel_url = await detect_tunnel_url(timeout=30)
        if tunnel_url:
            os.environ["A2A_BASE_URL"] = tunnel_url
            # Regenerate agent card with the real public URL
            try:
                from app.a2a_server import refresh_agent_card
                refresh_agent_card()
            except Exception as e:
                logger.warning("Could not refresh agent card: %s", e)
            # Broadcast new URL to all friends (retries in background for slow friends)
            try:
                from app.tools.a2a import perform_a2a_broadcast
                result = await perform_a2a_broadcast()
                logger.info("A2A broadcast result: %s", result.get("message", result.get("status")))
            except Exception as e:
                logger.error("A2A broadcast failed: %s", e)
        else:
            logger.warning("Tunnel URL not detected — skipping A2A broadcast")
    tasks.append(asyncio.create_task(detect_and_broadcast()))

    # 3. Telegram Interface (Core)
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        try:
            from interfaces.telegram_poller import poll_telegram
            tasks.append(asyncio.create_task(poll_telegram(get_runner, process_init_command)))
        except (ImportError, ModuleNotFoundError) as e:
            logger.error(f"Telegram dependencies missing: {e}")

    # 4. Slack Interface (Optional Integration)
    if os.environ.get("SLACK_BOT_TOKEN"):
        try:
            # Lazy load Slack poller to prevent bricking if slack-bolt is missing
            from interfaces.slack_poller import poll_slack
            tasks.append(asyncio.create_task(poll_slack(get_runner, process_init_command)))
        except (ImportError, ModuleNotFoundError) as e:
            logger.warning("Slack dependencies (slack-bolt) not found. Slack integration disabled.")
        except Exception as e:
            logger.error(f"Slack poller failed to initialize: {e}")

    # 5. CLI Chat Interface (Fallback)
    # If no messaging interfaces are active, start the CLI interface.
    # This allows the user to interact with the bot directly from the terminal.
    if not any(key in os.environ for key in ["TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN"]):
        try:
            from interfaces.cli_chat import start_cli_chat
            tasks.append(asyncio.create_task(start_cli_chat(get_runner)))
        except (ImportError, ModuleNotFoundError) as e:
            logger.error(f"CLI Chat dependencies missing: {e}")

    if tasks:
        logger.info("Bot is active and listening on configured channels.")
    else:
        logger.warning("No messaging interfaces active. Bot is effectively silent.")

    try:
        # Wait for tasks, cancelling all when an exit signal is detected.
        # One-shot tasks (broadcast, warnings) complete early without a signal.
        # When a transport (telegram/slack/cli) exits due to an exit signal,
        # we cancel remaining tasks (e.g. uvicorn) so the process can exit.
        from app.tools.system import check_exit_signal
        remaining = set(tasks)
        while remaining:
            done, remaining = await asyncio.wait(remaining, return_when=asyncio.FIRST_COMPLETED)
            if check_exit_signal():
                for t in remaining:
                    t.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)
                break
    except asyncio.CancelledError:
        logger.info("Daemon shutting down.")
    finally:
        scheduler.shutdown()
        
        # Clear crash file on clean exit (0), update (100) or rollback (101)
        crash_file = os.environ.get("CRASH_FILE", "./data/.crash_count")
        try:
            with open(crash_file, "w") as f:
                f.write("0\n")
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main())

    # Exit with the actual signal code so the supervisor gets it from
    # proc.returncode directly — not just from the signal file (race-proof).
    signal_file = os.path.abspath("./data/.exit_signal")
    if os.path.exists(signal_file):
        try:
            with open(signal_file) as f:
                code = int(f.read().strip())
            if code in (100, 101):
                sys.exit(code)
        except (ValueError, OSError):
            pass
