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
job_defaults = {
    "misfire_grace_time": 3600,  # 1 hour grace time for missed jobs during reboots
    "coalesce": True,
    "max_instances": 1
}
scheduler = AsyncIOScheduler(jobstores=jobstores, job_defaults=job_defaults)
