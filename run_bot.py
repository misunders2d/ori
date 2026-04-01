import asyncio
import logging
import os
import secrets
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

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(), RotatingFileHandler(LOG_FILE_PATH, maxBytes=100_000, backupCount=1)]
)
logger = logging.getLogger(__name__)

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
    """Corrects file permissions in the data directory at startup."""
    data_dir = os.path.abspath("./data")
    if not os.path.exists(data_dir): return
    try:
        os.chmod(data_dir, 0o777)
        for item in os.listdir(data_dir):
            path = os.path.join(data_dir, item)
            try: os.chmod(path, 0o666 if not os.path.isdir(path) else 0o777)
            except Exception: pass
        logger.info("FileSystem: Permissions self-healed.")
    except Exception as e: logger.warning(f"FileSystem: Self-heal limited: {e}")

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
    runner = get_runner()
    scheduler.start()
    tasks = []
    
    # 1. A2A Native Server
    try:
        import uvicorn
        from app.a2a_server import a2a_app
        if a2a_app:
            port = int(os.environ.get("A2A_PORT", 8000))
            config = uvicorn.Config(a2a_app, host="0.0.0.0", port=port, log_level="info", proxy_headers=True, forwarded_allow_ips="*")
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
            logger.warning(f"Slack dependencies (slack-bolt) not found. Slack integration disabled. Error: {e}")
        except Exception as e:
            logger.error(f"Slack poller failed to initialize: {e}")

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

if __name__ == "__main__":
    asyncio.run(main())
