"""Inspect a scheduled APScheduler job's kwargs — especially whether plan
enforcement (`steps`) is wired up.

Usage from the repo root:

    uv run python scripts/inspect_scheduled_task.py <job_id>
    uv run python scripts/inspect_scheduled_task.py cron_2125f003

Prints all top-level kwargs, flags whether `steps` enforcement is active,
and shows the step list if present.
"""

import asyncio
import sys

from deploy.vault import load_vault

load_vault()

from app.scheduler_instance import scheduler  # noqa: E402


async def _inspect(job_id: str) -> int:
    scheduler.start(paused=True)
    try:
        job = scheduler.get_job(job_id)
        if job is None:
            print(f"Job {job_id!r} not found.")
            return 1

        kwargs = job.kwargs or {}
        print(f"job_id:       {job.id}")
        print(f"next_run:     {job.next_run_time.isoformat() if job.next_run_time else '—'}")
        print(f"trigger:      {job.trigger}")
        print(f"kwargs keys:  {sorted(kwargs.keys())}")
        print()

        prompt = (kwargs.get("task_prompt") or "").strip()
        if prompt:
            preview = prompt if len(prompt) <= 400 else prompt[:400] + "…"
            print("task_prompt:")
            print(f"  {preview}")
            print()

        steps = kwargs.get("steps")
        if steps:
            print(f"PLAN ENFORCEMENT: ACTIVE ({len(steps)} steps)")
            for i, s in enumerate(steps, 1):
                print(f"  {i}. {s}")
        else:
            print("PLAN ENFORCEMENT: NOT WIRED UP (no `steps` kwarg).")
            print("  Task runs under normal LLM-decided flow — the spreadsheet")
            print("  Plan tab is documentation only, not hard enforcement.")
        return 0
    finally:
        scheduler.shutdown(wait=False)


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: uv run python scripts/inspect_scheduled_task.py <job_id>")
        sys.exit(2)
    sys.exit(asyncio.run(_inspect(sys.argv[1])))


if __name__ == "__main__":
    main()
