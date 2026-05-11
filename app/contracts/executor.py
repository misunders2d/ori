"""Scheduler integration — register / fire / unregister contracts as
APScheduler jobs.

Legacy ``schedule_recurring_task`` jobs keep their existing callback
and id prefix (``cron_*``). Contract-driven jobs use a different
prefix (``contract:<id>``) and a different callback so the two paths
coexist without stepping on each other (2026-05-11 incident: jumping
the gun and migrating jobs in bulk is what we want to avoid).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from apscheduler.triggers.cron import CronTrigger as APSCronTrigger

from app.contracts.schema import Contract, CronTrigger, EventTrigger, OnDemandTrigger
from app.contracts.store import contract_store
from app.contracts.worker import execute_contract

logger = logging.getLogger(__name__)


_CONTRACT_JOB_PREFIX = "contract:"


def contract_job_id(contract_id: str) -> str:
    """APScheduler job id reserved for a given contract. One job per
    contract id — revisions update the same job (via remove + add)."""
    return f"{_CONTRACT_JOB_PREFIX}{contract_id}"


# ---------------------------------------------------------------------------
# Scheduler-callable: invoked by APScheduler when a contract fires
# ---------------------------------------------------------------------------


def run_contract_fire(contract_id: str, hash_: str) -> None:
    """Top-level callable for APScheduler. Synchronous wrapper that
    spins up an event loop for the async worker.

    Stored on the job by id-and-hash (not by closure over a Contract
    object) so a worker restart picks the latest disk state — but the
    hash pin still defends against a job firing against a body it
    didn't authorise. If the hash on disk no longer matches, the
    worker raises ``ContractFireError`` and the job continues to be
    scheduled (admin must investigate).
    """
    try:
        contract = contract_store.load(contract_id, hash_)
    except Exception as e:
        logger.error("contract fire failed to load %s@%s: %s", contract_id, hash_, e)
        return

    try:
        asyncio.run(execute_contract(contract))
    except Exception as e:
        logger.exception("contract %s fire crashed: %s", contract_id, e)


# ---------------------------------------------------------------------------
# Schedule / unschedule
# ---------------------------------------------------------------------------


def schedule_contract(contract: Contract) -> dict:
    """Register the contract with APScheduler.

    Replaces any existing job for the same contract id (so revising +
    re-scheduling is one atomic switch). ``OnDemandTrigger`` contracts
    are NOT added to APScheduler — they fire only via direct
    invocation (``run_contract_fire`` called from a tool). ``EventTrigger``
    contracts are also skipped here; the event-bus wiring is a follow-up.

    Returns ``{"status": "scheduled"|"on_demand"|"error", ...}``.
    """
    from app.scheduler_instance import scheduler

    job_id = contract_job_id(contract.id)

    # Remove any prior job for the same contract id (revisions).
    try:
        scheduler.remove_job(job_id)
        logger.info("Removed prior APScheduler job %s before re-add", job_id)
    except Exception:
        pass  # no prior job is fine

    if isinstance(contract.trigger, OnDemandTrigger):
        return {
            "status": "on_demand",
            "message": (
                f"Contract {contract.id!r} is on-demand — not added to "
                f"APScheduler. Invoke directly via the contract runner."
            ),
        }

    if isinstance(contract.trigger, EventTrigger):
        return {
            "status": "event",
            "message": (
                f"Contract {contract.id!r} uses an event trigger "
                f"({contract.trigger.event!r}) — event-bus wiring is a "
                "follow-up. Not added to APScheduler."
            ),
        }

    assert isinstance(contract.trigger, CronTrigger)

    aps_trigger = APSCronTrigger.from_crontab(
        contract.trigger.cron, timezone=contract.trigger.timezone
    )

    scheduler.add_job(
        run_contract_fire,
        trigger=aps_trigger,
        kwargs={"contract_id": contract.id, "hash_": contract.hash},
        id=job_id,
        replace_existing=True,
    )

    return {
        "status": "scheduled",
        "job_id": job_id,
        "cron": contract.trigger.cron,
        "timezone": contract.trigger.timezone,
        "hash": contract.hash,
    }


def unschedule_contract(contract_id: str) -> dict:
    """Remove the contract's APScheduler job (if any). The frozen
    contract body stays on disk and remains loadable by hash — only
    the active schedule is dropped."""
    from app.scheduler_instance import scheduler

    job_id = contract_job_id(contract_id)
    try:
        scheduler.remove_job(job_id)
        return {"status": "removed", "job_id": job_id}
    except Exception as e:
        return {"status": "not_found", "job_id": job_id, "error": str(e)}


def list_contract_jobs() -> list[dict]:
    """Return all APScheduler jobs whose id matches the contract
    prefix, with their next-run-time and bound hash."""
    from app.scheduler_instance import scheduler

    out: list[dict] = []
    for job in scheduler.get_jobs():
        if not str(job.id).startswith(_CONTRACT_JOB_PREFIX):
            continue
        out.append(
            {
                "job_id": job.id,
                "contract_id": str(job.id)[len(_CONTRACT_JOB_PREFIX):],
                "hash": job.kwargs.get("hash_") if job.kwargs else None,
                "next_run": str(getattr(job, "next_run_time", "unknown")),
            }
        )
    return out
