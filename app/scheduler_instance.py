import os

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Use the same database URL as the ADK SessionService, but ensure it's synchronous
db_url = os.environ.get("DATABASE_URL", "sqlite:///./data/ori.db")

# APScheduler 3.x requires a synchronous driver. If the URL contains '+aiosqlite', strip it.
if "+aiosqlite" in db_url:
    db_url = db_url.replace("+aiosqlite", "")

# Ensure the database directory exists if using SQLite
if db_url.startswith("sqlite:///"):
    db_path = db_url.replace("sqlite:///", "")
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)

# Configure the scheduler to use our database for persistent job storage
jobstores = {"default": SQLAlchemyJobStore(url=db_url, tablename="apscheduler_jobs")}

# Instantiate the scheduler (it will be started by the FastAPI lifespan in main.py)
scheduler = AsyncIOScheduler(jobstores=jobstores)
