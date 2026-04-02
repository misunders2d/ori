import asyncio
import logging
import os
import secrets
import sqlite3
import sys
from dotenv import load_dotenv, set_key

# Load env variables safely
ENV_FILE_PATH = os.environ.get("DOTENV_PATH", "./data/.env")
os.makedirs(os.path.dirname(ENV_FILE_PATH), exist_ok=True)
if not os.path.exists(ENV_FILE_PATH):
    with open(ENV_FILE_PATH, "w") as f: f.write("# Ori Daemon Configuration\n")
load_dotenv(ENV_FILE_PATH, override=True)

# Generate keys
if not os.environ.get("ADMIN_PASSCODE"):
    set_key(ENV_FILE_PATH, "ADMIN_PASSCODE", secrets.token_urlsafe(16))
if not os.environ.get("A2A_API_KEY"):
    set_key(ENV_FILE_PATH, "A2A_API_KEY", "ori-" + secrets.token_urlsafe(24))

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
        if not os.environ.get("GOOGLE_API_KEY"): return None
        db_path = os.path.abspath("./data/ori-sessions.db")
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

def ensure_writable_data():
    """Aggressively corrects file permissions in the data directory at startup to prevent 'readonly' errors."""
    data_dir = os.path.abspath("./data")
    if not os.path.exists(data_dir): return
    try:
        # Recursive chmod to ensure SQLite can always write journals and wal files
        # Using 777/666 to ensure write access regardless of UID/GID alignment during complex filesystem syncs
        os.chmod(data_dir, 0o777)
        for root, dirs, files in os.walk(data_dir):
            for d in dirs:
                try: os.chmod(os.path.join(root, d), 0o777)
                except Exception: pass
            for f in files:
                try: os.chmod(os.path.join(root, f), 0o666)
                except Exception: pass
        logger.info("FileSystem: Permissions self-healed and locked (777/666).")
    except Exception as e: logger.warning(f"FileSystem: Self-heal limited: {e}")

def ensure_db_concurrency():
    """Enables SQLite Write-Ahead Logging (WAL) for better concurrency and fewer 'database is locked' errors."""
    data_dir = os.path.abspath("./data")
    if not os.path.exists(data_dir): return
    for f in os.listdir(data_dir):
        if f.endswith(".db"):
            db_path = os.path.join(data_dir, f)
            try:
                # Use a standard sync connection to set the journal mode
                with sqlite3.connect(db_path) as conn:
                    conn.execute("PRAGMA journal_mode=WAL;")
                logger.info(f"Database: Concurrency optimized for {f} (WAL mode).")
            except Exception as e:
                logger.warning(f"Database: Could not optimize {f}: {e}")

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
    ensure_writable_data()
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
            tasks.append(asyncio.create_task(uvicorn.Server(config).serve()))
    except Exception as e:
        logger.warning(f"A2A Server failed to start: {e}")

    # 2. Automatic A2A Broadcast
    async def broadcast_later():
        await asyncio.sleep(5)
        try:
            from app.tools.a2a import perform_a2a_broadcast
            await perform_a2a_broadcast()
        except Exception: pass
    tasks.append(asyncio.create_task(broadcast_later()))

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
        await asyncio.gather(*tasks)
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
