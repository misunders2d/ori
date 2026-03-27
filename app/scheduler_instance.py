import os

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Dedicated database for APScheduler job persistence (separate from ADK sessions)
db_url = os.environ.get("SCHEDULER_DATABASE_URL", "sqlite:///./data/ori-scheduler.db")

# Ensure the database directory exists if using SQLite
if db_url.startswith("sqlite:///"):
    db_path = db_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

jobstores = {"default": SQLAlchemyJobStore(url=db_url, tablename="apscheduler_jobs")}
scheduler = AsyncIOScheduler(jobstores=jobstores)
