import subprocess
import logging
import os
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

UPSTREAM_URL = "https://github.com/misunders2d/ori.git"

def _run_git(args: List[str], timeout: int = 30) -> str:
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True
    )
    return result.stdout.strip()

async def get_upstream_status() -> Dict[str, Any]:
    """
    Fetches the status of the upstream repository and compares it with the local branch.
    Returns a summary of differences (commits, new files, etc.)
    """
    try:
        # 1. Ensure upstream remote exists
        try:
            _run_git(["remote", "add", "upstream", UPSTREAM_URL])
        except subprocess.CalledProcessError:
            # Remote might already exist
            pass
            
        # 2. Fetch latest from upstream
        _run_git(["fetch", "upstream", "master"])
        
        # 3. Get local and upstream hashes
        local_hash = _run_git(["rev-parse", "HEAD"])
        upstream_hash = _run_git(["rev-parse", "upstream/master"])
        
        if local_hash == upstream_hash:
            return {
                "status": "synced",
                "message": "Local codebase is fully synchronized with upstream Ori.",
                "commits": []
            }
            
        # 4. Get the list of commits that upstream has but we don't
        # Use a range to see what's new in upstream
        commits_raw = _run_git(["log", "HEAD..upstream/master", "--oneline", "--max-count=10"])
        commits = commits_raw.split("\n") if commits_raw else []
        
        # 5. Check for structural differences (new files/dirs)
        diff_files = _run_git(["diff", "HEAD..upstream/master", "--name-only", "--diff-filter=A"])
        new_files = diff_files.split("\n") if diff_files else []
        
        return {
            "status": "diverged",
            "message": f"Upstream Ori has {len(commits)} new commit(s) and {len(new_files)} new file(s).",
            "commits": commits,
            "new_files": new_files,
            "upstream_hash": upstream_hash
        }

    except Exception as e:
        logger.error("Failed to check upstream status: %s", e)
        return {
            "status": "error",
            "message": f"Could not reach upstream repository: {str(e)}"
        }

async def get_file_diff(file_path: str) -> str:
    """Returns the text diff of a specific file between local and upstream master."""
    try:
        return _run_git(["diff", "HEAD..upstream/master", "--", file_path])
    except Exception as e:
        return f"Error fetching diff for {file_path}: {str(e)}"
