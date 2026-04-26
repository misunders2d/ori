"""Migration: wipe user-scheduled APScheduler jobs from the jobstore.

The scheduled-task identity model now requires `owner_user_id` as an explicit
top-level kwarg on every user-scheduled job. Jobs persisted before this
contract can't be safely migrated (ownership for group-chat schedules isn't
recoverable), so the cleanest path is to wipe them and have each owner
reschedule from chat under the new code.

By default the script is a DRY RUN — it prints what would be deleted and
exits. Pass --wipe to actually delete.

System tasks (`sys_*` prefix) are never touched; they already carry
admin_user_id explicitly and the new contract doesn't change them.

Usage from the repo root:

    uv run python scripts/reset_scheduled_tasks.py           # dry run
    uv run python scripts/reset_scheduled_tasks.py --wipe    # delete
"""

import sys

# Hydrate vault first — matches how the bot itself boots, so the SQLAlchemy
# URL resolves the same way.
from deploy.vault import load_vault

load_vault()

from app.scheduler_instance import scheduler  # noqa: E402

# Any job whose id starts with one of these is a user-scheduled task under the
# old ownership model. Everything else (notably sys_*) is left alone.
USER_JOB_PREFIXES = ("sched_", "oneoff_", "cron_")


def _is_user_job(job_id: str) -> bool:
    return any(job_id.startswith(p) for p in USER_JOB_PREFIXES)


def _owner_from_kwargs(kwargs: dict) -> str:
    """Best-effort owner extraction for *reporting only* (not for migration)."""
    if not isinstance(kwargs, dict):
        return ""
    # New contract
    if kwargs.get("owner_user_id"):
        return kwargs["owner_user_id"]
    if kwargs.get("admin_user_id"):
        return kwargs["admin_user_id"]
    # Legacy stamping
    notify = kwargs.get("notify") or {}
    if isinstance(notify, dict):
        return notify.get("owner_user_id", "") or ""
    return ""


def main() -> None:
    wipe = "--wipe" in sys.argv

    # Start scheduler briefly (non-blocking) so the jobstore is loaded.
    scheduler.start(paused=True)
    try:
        jobs = scheduler.get_jobs()
    except Exception as exc:
        print(f"Failed to read jobstore: {exc}")
        scheduler.shutdown(wait=False)
        return

    user_jobs = [j for j in jobs if _is_user_job(j.id)]
    system_jobs = [j for j in jobs if not _is_user_job(j.id)]

    print(f"Found {len(jobs)} total job(s): "
          f"{len(user_jobs)} user, {len(system_jobs)} system.\n")

    if user_jobs:
        print("User-scheduled jobs (these will be deleted with --wipe):")
        for j in user_jobs:
            kwargs = j.kwargs or {}
            owner = _owner_from_kwargs(kwargs) or "<unknown>"
            prompt = (kwargs.get("task_prompt") or "")[:80].replace("\n", " ")
            next_run = j.next_run_time.isoformat() if j.next_run_time else "—"
            print(f"  {j.id}  owner={owner}  next={next_run}  prompt={prompt!r}")
        print()

    if system_jobs:
        print("System jobs (preserved, never deleted):")
        for j in system_jobs:
            next_run = j.next_run_time.isoformat() if j.next_run_time else "—"
            print(f"  {j.id}  next={next_run}")
        print()

    if not wipe:
        print("Dry run. Re-run with --wipe to delete the user-scheduled jobs above.")
        scheduler.shutdown(wait=False)
        return

    removed = 0
    for j in user_jobs:
        try:
            scheduler.remove_job(j.id)
            removed += 1
        except Exception as exc:
            print(f"  failed to remove {j.id}: {exc}")
    print(f"\nDeleted {removed}/{len(user_jobs)} user-scheduled job(s). "
          f"Ask each owner to reschedule from chat.")

    scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
