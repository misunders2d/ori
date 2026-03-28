import asyncio
import logging
import os
import secrets

from dotenv import load_dotenv, set_key

# Load env variables safely before starting Google API integrations
ENV_FILE_PATH = os.environ.get("DOTENV_PATH", "./data/.env")
os.makedirs(os.path.dirname(ENV_FILE_PATH), exist_ok=True)
if not os.path.exists(ENV_FILE_PATH):
    with open(ENV_FILE_PATH, "w") as f:
        f.write("# Ori Daemon Configuration\n")
load_dotenv(ENV_FILE_PATH, override=True)

# Generate a random ADMIN_PASSCODE on first start if none exists
if not os.environ.get("ADMIN_PASSCODE"):
    _generated_passcode = secrets.token_urlsafe(16)
    set_key(ENV_FILE_PATH, "ADMIN_PASSCODE", _generated_passcode)
    os.environ["ADMIN_PASSCODE"] = _generated_passcode

from logging.handlers import RotatingFileHandler
import logging

LOG_FILE_PATH = os.path.abspath("./data/agent.log")
os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(LOG_FILE_PATH, maxBytes=100_000, backupCount=1)
    ]
)
logger = logging.getLogger(__name__)

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService

from app.agent import app as ori_app
from app.scheduler_instance import scheduler
from interfaces.telegram_poller import poll_telegram

_global_runner = None


def get_runner():
    """Lazily initializes and returns the primary global ADK Runner."""
    global _global_runner
    if not _global_runner:
        if not os.environ.get("GOOGLE_API_KEY"):
            return None

        db_path = os.path.abspath("./data/ori-sessions.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        database_url = f"sqlite+aiosqlite:///{db_path}"

        session_service = DatabaseSessionService(db_url=database_url)

        _global_runner = Runner(
            app=ori_app,
            session_service=session_service,
        )
    return _global_runner


def process_init_command(text: str) -> str:
    """
    Parses the `/init <passcode>` command intercept from communication channels.
    Validates against the `.env` ADMIN_PASSCODE and unlocks agent routing.
    Forces an asynchronous runner reload on a successful auth to capture new keys.
    """
    from app.app_utils.config import update_config

    ADMIN_PASSCODE = os.environ.get("ADMIN_PASSCODE", "SETUP")
    result = update_config(text, admin_passcode=ADMIN_PASSCODE)
    if "updated" in result.lower():
        global _global_runner
        _global_runner = None  # Force a reload of environments on next tick
    return result


async def main():
    """
    The Master Entrypoint for the Docker application daemon.
    Initializes the SQLite Database session, launches the APScheduler background thread,
    and concurrently maps all polling interfaces (Telegram, Slack, etc.) to the ADK `Runner`.
    """
    logger.info("Initializing Autonomous Worker Daemon...")

    # 1. Warm up the runner
    runner = get_runner()
    if not runner:
        passcode = os.environ.get("ADMIN_PASSCODE", "SETUP")
        bot_name = os.environ.get("BOT_NAME", "Ori")
        logger.warning(
            "\n"
            "============================================================\n"
            "  %s — FIRST-TIME SETUP REQUIRED\n"
            "============================================================\n"
            "  No GOOGLE_API_KEY detected. The bot is running but cannot\n"
            "  process messages until you configure it.\n"
            "\n"
            "  Your admin passcode: %s\n"
            "\n"
            "  Open your Telegram bot and send:\n"
            "    /init %s GOOGLE_API_KEY=your-key-here\n"
            "\n"
            "  You can also set multiple keys at once:\n"
            "    /init %s GOOGLE_API_KEY=xxx GITHUB_TOKEN=yyy\n"
            "\n"
            "  Give your bot a custom name:\n"
            "    /init %s BOT_NAME=MyBot\n"
            "\n"
            "  Your passcode is stored in: %s\n"
            "  If you lose it, edit that file to reset ADMIN_PASSCODE.\n"
            "============================================================",
            bot_name, passcode, passcode, passcode, passcode,
            os.path.abspath(ENV_FILE_PATH),
        )

    # 2. Start the isolated APScheduler engine exactly once
    logger.info("Starting APScheduler engine...")
    scheduler.start()

    # 3. Register periodic database backups (every 12 hours, keep last 3)
    from app.core.backup import backup_database

    sessions_db = os.path.abspath("./data/ori-sessions.db")
    scheduler_db = os.path.abspath("./data/ori-scheduler.db")
    scheduler.add_job(
        backup_database, "interval", hours=12,
        kwargs={"db_path": sessions_db, "label": "sessions"},
        id="backup_sessions", replace_existing=True,
    )
    scheduler.add_job(
        backup_database, "interval", hours=12,
        kwargs={"db_path": scheduler_db, "label": "scheduler"},
        id="backup_scheduler", replace_existing=True,
    )

    # 4. Collect and spin up all polling interfaces
    tasks = []

    # Telegram
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        tasks.append(
            asyncio.create_task(poll_telegram(get_runner, process_init_command))
        )
    else:
        logger.warning("TELEGRAM_BOT_TOKEN missing. Telegram poller skipped.")

    # Future: Slack/Discord/Webex pollers can be added here identically.

    if not tasks:
        logger.warning("No communication pollers started. The bot has no way to receive inputs.")
    else:
        logger.info("Bot is fully active and listening on all configured channels!")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Daemon gracefully shutting down.")
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
