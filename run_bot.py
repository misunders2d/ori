import asyncio
import logging
import os
import secrets
import sys

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

async def run_proactive_diagnostics():
    """Background task that checks system health and alerts the admin if degraded."""
    from app.core.health import get_system_health
    from app.core.transport import get_adapter
    
    # We only alert if status is degraded
    try:
        report = await get_system_health()
        if report["status"] != "healthy":
            logger.warning("Proactive Diagnostics: System is %s. Vitals: %s", report["status"], report["vitals"])
            
            # Try to notify admin via Telegram if configured
            admin_ids = os.environ.get("ADMIN_USER_IDS", "").split(",")
            adapter = get_adapter("telegram")
            if adapter and admin_ids:
                for aid in admin_ids:
                    aid = aid.strip()
                    if not aid: continue
                    # Clean the raw ID if it starts with 'tg_'
                    raw_id = aid.replace("tg_", "")
                    try:
                        await adapter.send_message(
                            raw_id, 
                            f"🚨 **Proactive Alert: System Health {report['status'].upper()}**\n\n"
                            f"Vitals:\n" + "\n".join([f"- {k}: `{v}`" for k, v in report["vitals"].items()])
                        )
                    except Exception:
                        pass
    except Exception as e:
        logger.error("Proactive diagnostics task failed: %s", e)

async def _send_startup_notification():
    """Checks for a pending update trigger and notifies the user upon successful restart."""
    import json
    trigger_path = os.path.abspath("./data/.update_trigger")
    if os.path.exists(trigger_path):
        try:
            with open(trigger_path, "r") as f:
                data = json.load(f)
            
            notify = data.get("notify", {})
            if notify:
                # Wait briefly for adapters to register
                await asyncio.sleep(2)
                from app.core.transport import get_adapter
                adapter = get_adapter(notify.get("type"))
                if adapter:
                    target = notify.get("chat_id") or notify.get("channel")
                    bot_name = os.environ.get("BOT_NAME", "Ori")
                    await adapter.send_message(
                        target, 
                        f"✅ **{bot_name} is back online!**\n\n"
                        "The system has successfully rebooted and is ready for commands."
                    )
            
            # Remove the trigger so it doesn't fire again
            os.remove(trigger_path)
        except Exception as e:
            logger.error("Failed to send startup notification: %s", e)

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

    # 3. Register periodic jobs
    from app.core.backup import backup_database

    sessions_db = os.path.abspath("./data/ori-sessions.db")
    scheduler_db = os.path.abspath("./data/ori-scheduler.db")
    
    # Backups
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
    
    # Self-Diagnostics (every 10 minutes)
    scheduler.add_job(
        run_proactive_diagnostics, "interval", minutes=10,
        id="self_diagnostics", replace_existing=True,
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

    # 5. Startup Notification (Async)
    asyncio.create_task(_send_startup_notification())

    # Future: Slack/Discord/Webex pollers can be added here identically.

    if not tasks:
        # If no messengers are configured AND we are in an interactive terminal, start CLI onboarding
        if sys.stdin.isatty():
            from interfaces.cli_chat import start_cli_chat
            logger.info("No communication pollers started. Launching interactive CLI onboarding...")
            tasks.append(asyncio.create_task(start_cli_chat(get_runner)))
        else:
            logger.warning("No communication pollers started and no interactive TTY detected.")
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
