import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _is_child_container() -> bool:
    """Detect if we're running as a spawned child (no .git, no launcher).

    `.git` in a worktree is a file (pointer to the parent), not a directory.
    `os.path.exists` covers both shapes; `os.path.isdir` would misidentify
    a worktree as a child container and disable self-evolution. See the
    May 2026 rescue retrospective.
    """
    return not os.path.exists(os.path.join(PROJECT_ROOT, ".git"))


# ---------------------------------------------------------------------------
# Sandbox cycle marker (disk-backed, survives /reset session + restarts).
#
# Replaces the older `tool_context.state["evolution_cycle_active"]` flag,
# which was session-scoped and vanished on a session reset mid-cycle —
# leaving the sandbox content orphaned and tripping spurious "stale
# sandbox" wipes on the next stage.
#
# The marker is a tiny file at `<sandbox_dir>/.cycle_active`. Its mtime
# is the cycle's start time. Cycles older than `_SANDBOX_CYCLE_TTL_SECONDS`
# (24h default) are considered abandoned and auto-discarded by the next
# `evolution_stage_change` via `_sandbox_cycle_begin`.
# ---------------------------------------------------------------------------

_SANDBOX_CYCLE_TTL_SECONDS = 24 * 3600


def _sandbox_cycle_marker_path(sandbox_dir: str) -> str:
    return os.path.join(sandbox_dir, ".cycle_active")


def _sandbox_cycle_is_fresh(sandbox_dir: str) -> bool:
    """True if a marker exists and is within TTL."""
    marker = _sandbox_cycle_marker_path(sandbox_dir)
    if not os.path.isfile(marker):
        return False
    try:
        age = time.time() - os.path.getmtime(marker)
    except OSError:
        return False
    return age < _SANDBOX_CYCLE_TTL_SECONDS


def _sandbox_cycle_begin(sandbox_dir: str) -> None:
    """Start a fresh cycle: wipe + recreate the dir, write the marker."""
    if os.path.exists(sandbox_dir):
        shutil.rmtree(sandbox_dir, ignore_errors=True)
    os.makedirs(sandbox_dir, exist_ok=True)
    try:
        with open(_sandbox_cycle_marker_path(sandbox_dir), "w") as f:
            f.write(str(int(time.time())))
    except OSError as e:
        logger.warning("sandbox cycle marker write failed: %s", e)


def _sandbox_cycle_end(sandbox_dir: str) -> None:
    """End the cycle: wipe the sandbox + remove the marker."""
    if os.path.exists(sandbox_dir):
        shutil.rmtree(sandbox_dir, ignore_errors=True)


def _find_uv() -> str:
    """Find the uv binary, checking common install locations."""
    for candidate in [
        shutil.which("uv"),
        os.path.expanduser("~/.local/bin/uv"),
        os.path.expanduser("~/.cargo/bin/uv"),
        "/usr/local/bin/uv",
        "/usr/bin/uv",
    ]:
        if candidate and os.path.isfile(candidate):
            return candidate
    return "uv"  # fallback to PATH


def _safe_resolve_path(file_path: str, base_dir: str) -> str | None:
    """Resolve file_path relative to base_dir and ensure it stays within it."""
    base = os.path.abspath(base_dir)
    resolved = os.path.abspath(os.path.join(base, file_path))
    if not resolved.startswith(base + os.sep) and resolved != base:
        return None
    return resolved


def _sandbox_digest(sandbox_dir: str) -> str:
    """Return a stable digest of real staged files in the sandbox."""
    digest = hashlib.sha256()
    for src, rel in sorted(_collect_staged_files(sandbox_dir), key=lambda item: item[1]):
        digest.update(rel.encode("utf-8"))
        with open(src, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Audit log (Phase 5.1)
# ---------------------------------------------------------------------------
# Append-only JSONL at data/evolution_audit.jsonl. One line per
# stage/verify/commit/discard/rollback event. JSONL keeps the log
# greppable + tailable without a schema migration; failures swallow
# (with a warning) so audit issues never block an evolution itself.

_EVOLUTION_AUDIT_PATH = os.path.abspath("./data/evolution_audit.jsonl")


def _evolution_audit(
    phase: str,
    actor: str,
    status: str,
    *,
    files: list[str] | None = None,
    digest: str | None = None,
    error: str | None = None,
    **extra,
) -> None:
    """Append one event to data/evolution_audit.jsonl.

    `phase` ∈ {stage, verify, commit, discard, rollback}.
    `status` ∈ {ok, fail, noop}.
    """
    event = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "phase": phase,
        "actor": actor or "",
        "status": status,
    }
    if files is not None:
        event["files"] = files
    if digest is not None:
        event["digest"] = digest
    if error is not None:
        event["error"] = error
    if extra:
        event.update(extra)
    try:
        os.makedirs(os.path.dirname(_EVOLUTION_AUDIT_PATH), exist_ok=True)
        with open(_EVOLUTION_AUDIT_PATH, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception as e:
        logger.warning("evolution_audit write failed (%s): %s", phase, e)


def _audit_actor(tool_context: ToolContext | None) -> str:
    """Best-effort actor extraction from the tool_context for audit logs."""
    if not tool_context:
        return ""
    state = getattr(tool_context, "state", None)
    if state is None:
        return ""
    try:
        data = state.to_dict() if hasattr(state, "to_dict") else dict(state)
    except Exception:
        return ""
    return str(data.get("user_id") or "")



def evolution_read_file(file_path: str, tool_context: ToolContext) -> dict:
    """Reads the content of a file from the current agent's source code.

    Use this to understand your own code before proposing improvements.

    Args:
        file_path (str): The relative path to the file (e.g., 'app/agent.py').

    Returns:
        dict: Content of the file or error.
    """
    resolved = _safe_resolve_path(file_path, PROJECT_ROOT)
    if resolved is None:
        return {"status": "error", "message": "Path traversal denied. Use relative paths within the project."}
    if os.path.basename(resolved) == ".env" or file_path.endswith(".env"):
        return {"status": "error", "message": "Security error: Reading .env directly is blocked. Do not read credentials directly."}
    try:
        with open(resolved) as f:
            return {"status": "success", "content": f.read()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def evolution_list_directory(dir_path: str, tool_context: ToolContext) -> dict:
    """Lists the files and folders inside a specific directory of the agent's source code.

    Use this to explore the project structure (e.g., passing '.' for the root directory, or 'app/tools' for the tools folder).

    Args:
        dir_path (str): The relative path to the directory (e.g., '.', 'app', 'skills').

    Returns:
        dict: A list of files and folders in the directory, or an error message.
    """
    resolved = _safe_resolve_path(dir_path, PROJECT_ROOT)
    if resolved is None:
        return {"status": "error", "message": "Path traversal denied. Use relative paths within the project."}
    
    try:
        if not os.path.isdir(resolved):
            return {"status": "error", "message": f"Path is not a directory: {dir_path}"}
            
        items = os.listdir(resolved)
        # Sort for deterministic output: folders first, then files
        items.sort(key=lambda x: (not os.path.isdir(os.path.join(resolved, x)), x.lower()))
        
        output = []
        for item in items:
            # Skip noise
            if item in {".git", "__pycache__", ".pytest_cache"}:
                continue
                
            item_path = os.path.join(resolved, item)
            if os.path.isdir(item_path):
                output.append(f"📁 {item}/")
            else:
                output.append(f"📄 {item}")
                
        return {
            "status": "success",
            "directory": dir_path,
            "contents": output
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}



def evolution_stage_change(
    file_path: str, new_content: str, tool_context: ToolContext
) -> dict:
    """Stages a code change in a protected sandbox environment.

    This does NOT modify the live running code. It prepares the change for verification.

    IMPORTANT: You MUST NOT stage changes that remove, modify, or bypass system guardrails
    (event callbacks like `before_agent_callback`, `before_model_callback`, etc.)
    unless explicitly requested by the user.

    Args:
        file_path (str): The relative path to the file to modify.
        new_content (str): The full new content of the file.

    Returns:
        dict: Status of the staging operation.
    """
    if file_path.endswith(".env"):
        return {"status": "error", "message": "Security error: Writing to .env directly is blocked. Instruct human to configure integrations properly."}

    tool_context.state["evolution_verified_digest"] = ""

    # Deployment and recovery logic lives under deploy/ and must remain
    # untouched by self-evolution to prevent bricking the instance.
    if file_path.startswith("deploy/") or file_path.startswith("deploy\\"):
        return {"status": "error", "message": f"Deploy directory '{file_path}' is protected. Deployment and recovery scripts cannot be modified by self-evolution."}

    sandbox_dir = os.path.abspath("./data/sandbox")

    # Clear stale sandboxes from prior (abandoned, expired, or
    # rejected) cycles. Disk-backed marker survives /reset session
    # and process restarts — see helpers near the top of this module.
    if not _sandbox_cycle_is_fresh(sandbox_dir):
        _sandbox_cycle_begin(sandbox_dir)

    resolved = _safe_resolve_path(file_path, sandbox_dir)
    if resolved is None:
        return {"status": "error", "message": "Path traversal denied. Use relative paths within the sandbox."}

    os.makedirs(os.path.dirname(resolved), exist_ok=True)

    try:
        with open(resolved, "w") as f:
            f.write(new_content)
        _evolution_audit(
            "stage", _audit_actor(tool_context), "ok", files=[file_path],
        )
        return {
            "status": "success",
            "message": f"Staged changes for {file_path} in sandbox.",
        }
    except Exception as e:
        _evolution_audit(
            "stage", _audit_actor(tool_context), "fail",
            files=[file_path], error=str(e),
        )
        return {"status": "error", "message": str(e)}



def evolution_discard_sandbox(tool_context: ToolContext) -> dict:
    """Abandons the current self-evolution cycle, wiping the sandbox.

    Call this when:
    - You staged a change you no longer want to commit.
    - Verification keeps failing and you want to start clean.
    - You want to free disk space from an old cycle.

    Removes both the staged files and the cycle marker, so the next
    `evolution_stage_change` will start a fresh cycle.

    Returns:
        dict: {"status": "success" | "noop", "message": ...}
    """
    sandbox_dir = os.path.abspath("./data/sandbox")
    if not os.path.exists(sandbox_dir):
        _evolution_audit("discard", _audit_actor(tool_context), "noop")
        return {"status": "noop", "message": "No sandbox to discard."}

    _sandbox_cycle_end(sandbox_dir)
    # Clear the verification digest too — there's nothing left to commit.
    try:
        tool_context.state["evolution_verified_digest"] = ""
    except Exception:
        pass

    _evolution_audit("discard", _audit_actor(tool_context), "ok")
    return {"status": "success", "message": "Sandbox cycle discarded. Staged files and marker removed."}



def evolution_verify_sandbox(
    check: str, tool_context: ToolContext, target: str = ""
) -> dict:
    """Runs verification checks on staged sandbox changes.

    ALWAYS run 'syntax' check after staging a Python file, then 'pytest' for full tests.

    Args:
        check (str): The type of check to run. One of:
            - 'syntax' — Parse a Python file for syntax errors (requires target).
            - 'deps' — Resolve dependencies when pyproject.toml is staged. Runs uv lock and stages uv.lock.
            - 'pytest' — Run the full test suite.
            - 'import' — Try importing a module (requires target, e.g., 'app.tools').
        target (str): The file path (for 'syntax') or module name (for 'import'). Not needed for 'pytest' or 'deps'.

    Returns:
        dict: Verification status and output.
    """
    sandbox_dir = os.path.abspath("./data/sandbox")
    if not os.path.exists(sandbox_dir):
        return {"status": "error", "message": "No staged changes found in sandbox."}

    try:
        if check == "syntax":
            if not target:
                return {"status": "error", "message": "Target file path required for syntax check."}
            resolved = _safe_resolve_path(target, sandbox_dir)
            if resolved is None:
                return {"status": "error", "message": "Path traversal denied."}

            result = subprocess.run(
                [sys.executable, "-m", "py_compile", resolved],
                capture_output=True, text=True, timeout=15,
            )

        elif check == "deps":
            # Resolve AND install dependencies when pyproject.toml is staged.
            # Generates updated uv.lock and verifies uv sync succeeds, so the
            # Docker build (uv sync --frozen) won't fail after commit.
            staged_pyproject = os.path.join(sandbox_dir, "pyproject.toml")
            if not os.path.exists(staged_pyproject):
                return {"status": "error", "message": "No pyproject.toml staged. Stage it first, then run 'deps' check."}

            # uv lock needs the full project context — symlink everything else
            # IMPORTANT: Remove any existing uv.lock symlink first to prevent
            # writing through it and corrupting the live lockfile.
            sandbox_lock = os.path.join(sandbox_dir, "uv.lock")
            if os.path.islink(sandbox_lock):
                os.unlink(sandbox_lock)

            for item in os.listdir(PROJECT_ROOT):
                if item.startswith('.') or item == "data":
                    continue
                src = os.path.join(PROJECT_ROOT, item)
                dst = os.path.join(sandbox_dir, item)
                if not os.path.exists(dst):
                    try:
                        os.symlink(src, dst, target_is_directory=os.path.isdir(src))
                    except Exception:
                        pass

            # Step 1: Resolve dependencies (generates uv.lock)
            result = subprocess.run(
                [_find_uv(), "lock"],
                cwd=sandbox_dir,
                capture_output=True, text=True, timeout=120,
            )

            if result.returncode != 0:
                combined = (result.stdout or "") + "\n" + (result.stderr or "")
                return {
                    "status": "error",
                    "message": "Dependency resolution FAILED (uv lock).",
                    "output": combined[-1000:],
                }

            # Step 2: Verify installation works (catches missing system libs, build failures)
            sync_result = subprocess.run(
                [_find_uv(), "sync", "--frozen"],
                cwd=sandbox_dir,
                capture_output=True, text=True, timeout=300,
            )

            if sync_result.returncode != 0:
                combined = (sync_result.stdout or "") + "\n" + (sync_result.stderr or "")
                return {
                    "status": "error",
                    "message": "Dependency installation FAILED (uv sync). The package resolves but cannot be installed — check for missing system libraries or build dependencies.",
                    "output": combined[-1000:],
                }

            # uv.lock is now a real file in sandbox (not a symlink) — it will be collected by _collect_staged_files
            return {
                "status": "success",
                "message": "Dependencies resolved and installation verified. uv.lock updated and staged.",
                "output": result.stdout[-500:] if result.stdout else "",
            }

        elif check == "import" or check == "pytest":
            # Auto-bootstrap: symlink project structure to backfill missing files
            links_created = []
            for item in os.listdir(PROJECT_ROOT):
                if item.startswith('.') or item == "data" or item == "tests":
                    continue
                src = os.path.join(PROJECT_ROOT, item)
                dst = os.path.join(sandbox_dir, item)
                
                # IMPROVED BOOTSTRAP: If directory exists (due to staging), symlink contents individually
                if os.path.isdir(src):
                    os.makedirs(dst, exist_ok=True)
                    for subitem in os.listdir(src):
                        sub_src = os.path.join(src, subitem)
                        sub_dst = os.path.join(dst, subitem)
                        if not os.path.exists(sub_dst):
                            try:
                                if os.path.isdir(sub_src):
                                    os.symlink(sub_src, sub_dst, target_is_directory=True)
                                else:
                                    os.symlink(sub_src, sub_dst)
                                links_created.append(sub_dst)
                            except Exception:
                                pass
                elif not os.path.exists(dst):
                    try:
                        os.symlink(src, dst)
                        links_created.append(dst)
                    except Exception:
                        pass

            if check == "import":
                if not target:
                    return {"status": "error", "message": "Module name required for import check."}

                check_script = "import sys, importlib; importlib.import_module(sys.argv[1]); print('Import OK')"
                result = subprocess.run(
                    [sys.executable, "-c", check_script, target],
                    cwd=sandbox_dir,
                    capture_output=True, text=True, timeout=15,
                )
            else: # pytest
                # Symlink tests specially to ensure we have the latest tests
                live_tests = os.path.join(PROJECT_ROOT, "tests")
                sandbox_tests = os.path.join(sandbox_dir, "tests")
                if os.path.isdir(live_tests):
                    os.makedirs(sandbox_tests, exist_ok=True)
                    for fname in os.listdir(live_tests):
                        if fname.startswith(".") or fname == "__pycache__":
                            continue
                        src = os.path.join(live_tests, fname)
                        dst = os.path.join(sandbox_tests, fname)
                        if os.path.isfile(src) and not os.path.exists(dst):
                            os.symlink(src, dst)
                            links_created.append(dst)

                # Regenerate docs/INDEX.md before tests so any structural
                # change in the sandbox shows up in the index — keeps the
                # auto-generated docs in lockstep with the code being
                # verified. Non-fatal: failures here log, tests still run.
                gen_docs_path = os.path.join(PROJECT_ROOT, "scripts", "gen_docs.py")
                if os.path.isfile(gen_docs_path):
                    try:
                        subprocess.run(
                            [sys.executable, gen_docs_path],
                            cwd=sandbox_dir,
                            env={**os.environ, "GEN_DOCS_ROOT": sandbox_dir},
                            capture_output=True, text=True, timeout=30,
                        )
                    except Exception as e:
                        logger.warning("gen_docs pre-pytest hook skipped: %s", e)

                pytest_script = (
                    "import pytest, sys, os; "
                    "os.environ['PYTHONPATH'] = os.getcwd(); "
                    "sys.exit(pytest.main(['tests', '-v', '-m', 'not infra']))"
                )
                result = subprocess.run(
                    [sys.executable, "-c", pytest_script],
                    cwd=sandbox_dir,
                    capture_output=True, text=True, timeout=120,
                )

            # Clean up symlinks
            for link in links_created:
                if os.path.islink(link):
                    os.unlink(link)
                elif os.path.isdir(link) and not os.path.islink(link):
                    # In case it was a directory we created (like 'tests')
                    # but we only want to remove it if it's empty now
                    try:
                        os.removedirs(link)
                    except Exception:
                        pass

        else:
            return {"status": "error", "message": f"Unknown check type: '{check}'. Use 'syntax', 'pytest', or 'import'."}

        if result.returncode == 0:
            digest = ""
            if check == "pytest":
                digest = _sandbox_digest(sandbox_dir)
                tool_context.state["evolution_verified_digest"] = digest
            _evolution_audit(
                "verify", _audit_actor(tool_context), "ok",
                check=check, digest=digest or None,
            )
            return {
                "status": "success",
                "message": f"Verification PASSED ({check}).",
                "output": result.stdout[-500:] if result.stdout else "",
            }
        else:
            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            _evolution_audit(
                "verify", _audit_actor(tool_context), "fail",
                check=check, error=combined[-300:],
            )
            return {
                "status": "error",
                "message": f"Verification FAILED ({check}).",
                "output": combined[-1000:],
            }
    except subprocess.TimeoutExpired:
        _evolution_audit(
            "verify", _audit_actor(tool_context), "fail",
            check=check, error="timeout",
        )
        return {"status": "error", "message": f"Verification timed out ({check})."}
    except Exception as e:
        _evolution_audit(
            "verify", _audit_actor(tool_context), "fail",
            check=check, error=str(e),
        )
        return {"status": "error", "message": f"Verification crashed: {e}"}



def _collect_staged_files(sandbox_dir: str) -> list[tuple[str, str]]:
    """Walk the sandbox and return [(abs_src, rel_path), ...] for real files (not symlinks)."""
    staged = []
    if not os.path.exists(sandbox_dir):
        return staged
    for root, _dirs, files in os.walk(sandbox_dir):
        rel_root = os.path.relpath(root, sandbox_dir)
        path_parts = rel_root.split(os.sep)
        if any(p.startswith('.') and p not in ['.', '..'] for p in path_parts) or "__pycache__" in path_parts:
            continue
        for fname in files:
            src = os.path.join(root, fname)
            if os.path.islink(src):
                continue
            rel = os.path.relpath(src, sandbox_dir)
            staged.append((src, rel))
    return staged


def _make_signed_message(commit_message: str) -> str:
    bot_name = os.environ.get("BOT_NAME", "Ori")
    signature = f"evolved by {bot_name}"
    if not commit_message.strip().endswith(signature):
        return f"{commit_message.strip()}\n\n{signature}"
    return commit_message.strip()


def _evolution_commit_remote(
    staged_files, delete_files, commit_message, skip_local_update
) -> dict:
    """Push changes via temporary clone to GitHub (remote evolution)."""
    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_repo = os.environ.get("GITHUB_REPO", "")

    tmp_repo_dir = f"/tmp/evolution_{uuid.uuid4().hex[:8]}"
    push_url = f"https://x-access-token:{github_token}@github.com/{github_repo}.git"
    bot_name = os.environ.get("BOT_NAME", "Ori")

    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", push_url, tmp_repo_dir],
            capture_output=True, text=True, check=True, timeout=60,
        )

        if delete_files:
            for rel_path in delete_files:
                target = os.path.join(tmp_repo_dir, rel_path)
                if os.path.exists(target):
                    subprocess.run(["git", "rm", "-f", rel_path], cwd=tmp_repo_dir, check=True)

        for src, rel in staged_files:
            dst = os.path.join(tmp_repo_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

        subprocess.run(["git", "config", "user.email", "agent@evolution.local"], cwd=tmp_repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", f"{bot_name} (Agent)"], cwd=tmp_repo_dir, check=True)

        if staged_files:
            subprocess.run(["git", "add", "."], cwd=tmp_repo_dir, check=True)

        signed_message = _make_signed_message(commit_message)
        subprocess.run(["git", "commit", "-m", signed_message], cwd=tmp_repo_dir, check=True)

        result = subprocess.run(
            ["git", "push", "origin", "HEAD:master"],
            cwd=tmp_repo_dir, capture_output=True, text=True, timeout=60,
        )

        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout)[-500:].replace(github_token, "***")
            return {"status": "error", "message": f"git push failed: {err_msg}"}

        # Update local files if safe to do so
        if not skip_local_update:
            _apply_to_live(staged_files, delete_files)

    except subprocess.CalledProcessError as e:
        err_msg = (e.stderr or e.stdout or str(e))[-500:].replace(github_token, "***")
        return {"status": "error", "message": f"Git error: {err_msg}"}
    except Exception as e:
        return {"status": "error", "message": f"Error during push: {e!s}"}
    finally:
        if os.path.exists(tmp_repo_dir):
            shutil.rmtree(tmp_repo_dir)

    return {"status": "success", "mode": "remote"}


def _evolution_commit_local(
    staged_files, delete_files, commit_message, skip_local_update
) -> dict:
    """Commit changes locally using a git worktree (never touches the live directory).

    Flow: create worktree on feature branch -> apply changes -> commit -> merge
    to master (git state only, not files on disk) -> clean up worktree.
    The supervisor applies file changes after the process exits cleanly.
    """
    bot_name = os.environ.get("BOT_NAME", "Ori")
    branch_name = f"evo/{uuid.uuid4().hex[:8]}"
    worktree_dir = os.path.abspath("./data/evo-work")
    signed_message = _make_signed_message(commit_message)

    # Clean up any stale worktree from a previous failed evolution
    if os.path.exists(worktree_dir):
        subprocess.run(["git", "worktree", "remove", "--force", worktree_dir],
                        cwd=PROJECT_ROOT, capture_output=True, text=True)
        if os.path.exists(worktree_dir):
            shutil.rmtree(worktree_dir, ignore_errors=True)

    try:
        # Create worktree on a new feature branch
        subprocess.run(
            ["git", "worktree", "add", worktree_dir, "-b", branch_name],
            cwd=PROJECT_ROOT, check=True, capture_output=True, text=True,
        )

        # Apply staged files to the worktree
        for src, rel in staged_files:
            dst = os.path.join(worktree_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

        # Apply deletions
        if delete_files:
            for rel_path in delete_files:
                target = os.path.join(worktree_dir, rel_path)
                if os.path.exists(target):
                    os.remove(target)
                subprocess.run(["git", "rm", "-f", "--ignore-unmatch", rel_path],
                                cwd=worktree_dir, capture_output=True, text=True)

        # Stage and commit inside the worktree
        subprocess.run(["git", "add", "-A"], cwd=worktree_dir, check=True,
                        capture_output=True, text=True)
        subprocess.run(
            ["git", "config", "user.email", "agent@evolution.local"],
            cwd=worktree_dir, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", f"{bot_name} (Agent)"],
            cwd=worktree_dir, capture_output=True, text=True,
        )
        subprocess.run(["git", "commit", "-m", signed_message], cwd=worktree_dir, check=True,
                        capture_output=True, text=True)

        # Merge the feature branch into master (updates git state, NOT files on disk)
        result = subprocess.run(
            ["git", "merge", "--no-ff", branch_name, "-m", f"merge: {signed_message}"],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )

        if result.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=PROJECT_ROOT, capture_output=True)
            subprocess.run(["git", "worktree", "remove", "--force", worktree_dir],
                            cwd=PROJECT_ROOT, capture_output=True)
            subprocess.run(["git", "branch", "-D", branch_name], cwd=PROJECT_ROOT, capture_output=True)
            return {"status": "error", "message": f"Merge conflict. Branch discarded.\n{result.stderr[-500:]}"}

        # Clean up worktree and branch
        subprocess.run(["git", "worktree", "remove", "--force", worktree_dir],
                        cwd=PROJECT_ROOT, capture_output=True)
        subprocess.run(["git", "branch", "-d", branch_name], cwd=PROJECT_ROOT, capture_output=True)

    except subprocess.CalledProcessError as e:
        # Clean up on failure
        subprocess.run(["git", "worktree", "remove", "--force", worktree_dir],
                        cwd=PROJECT_ROOT, capture_output=True)
        subprocess.run(["git", "branch", "-D", branch_name], cwd=PROJECT_ROOT, capture_output=True)
        err_msg = (e.stderr or e.stdout or str(e))[-500:]
        return {"status": "error", "message": f"Local evolution failed: {err_msg}"}
    except Exception as e:
        subprocess.run(["git", "worktree", "remove", "--force", worktree_dir],
                        cwd=PROJECT_ROOT, capture_output=True)
        subprocess.run(["git", "branch", "-D", branch_name], cwd=PROJECT_ROOT, capture_output=True)
        return {"status": "error", "message": f"Local evolution error: {e!s}"}

    return {"status": "success", "mode": "local", "branch": branch_name}


def _apply_to_live(staged_files, delete_files):
    """Copy staged files to the live project root and apply deletions."""
    if delete_files:
        for rel_path in delete_files:
            live_target = os.path.join(PROJECT_ROOT, rel_path)
            if os.path.exists(live_target):
                os.remove(live_target)
    for src, rel in staged_files:
        live_dst = os.path.join(PROJECT_ROOT, rel)
        os.makedirs(os.path.dirname(live_dst), exist_ok=True)
        shutil.copy2(src, live_dst)


def evolution_commit_and_push(
    commit_message: str, tool_context: ToolContext, delete_files: Optional[List[str]] = None, skip_local_update: bool = False
) -> dict:
    """Commits verified changes. Uses GitHub if configured, otherwise commits locally.

    ONLY call this after ALL verification checks pass.

    Remote mode (GitHub configured): clones, commits, pushes to remote, optionally updates local.
    Local mode (no GitHub): creates a feature branch, commits, merges to master. Safe rollback on failure.

    NOTE: On spawned child containers (no .git), commit is not available. Use `export_dna` instead
    to send verified changes back to the parent agent.

    IMPORTANT: You MUST NOT commit changes that remove, modify, or bypass system guardrails
    (event callbacks like `before_agent_callback`, `before_model_callback`, etc.)
    unless explicitly requested by the user.

    Args:
        commit_message (str): A descriptive message explaining the improvement.
        delete_files (Optional[List[str]]): List of relative paths to files that should be deleted.
        skip_local_update (bool): If True (remote mode only), pushes but does NOT update the local filesystem.
            A hard reboot (exit 100) will be required afterward to apply changes.
    """
    if _is_child_container():
        return {
            "status": "error",
            "message": (
                "COMMIT BLOCKED: You are running as a spawned child agent (no .git repository). "
                "Children cannot commit directly. Your workflow is: stage → verify → `export_dna` to send "
                "verified changes back to the parent agent. The parent will commit and reboot."
            ),
        }

    sandbox_dir = os.path.abspath("./data/sandbox")
    has_staged = os.path.exists(sandbox_dir) and any(
        os.path.isfile(os.path.join(root, f))
        for root, _, files in os.walk(sandbox_dir) for f in files
    )

    if not has_staged and not delete_files:
        return {"status": "error", "message": "Nothing to commit or delete."}

    # --- AUTO-RESOLVE DEPENDENCIES ---
    # If pyproject.toml is staged, automatically run uv lock + uv sync --frozen
    # to generate a matching uv.lock and verify installation. This prevents
    # committing a pyproject.toml without a matching lockfile, which would crash
    # the Docker build (uv sync --frozen fails on stale lockfile).
    staged_pyproject = os.path.join(sandbox_dir, "pyproject.toml")
    if os.path.isfile(staged_pyproject) and not os.path.islink(staged_pyproject):
        # Break any existing uv.lock symlink to avoid corrupting the live lockfile
        sandbox_lock = os.path.join(sandbox_dir, "uv.lock")
        if os.path.islink(sandbox_lock):
            os.unlink(sandbox_lock)

        # Symlink project context for uv to work
        for item in os.listdir(PROJECT_ROOT):
            if item.startswith('.') or item == "data":
                continue
            src = os.path.join(PROJECT_ROOT, item)
            dst = os.path.join(sandbox_dir, item)
            if not os.path.exists(dst):
                try:
                    os.symlink(src, dst, target_is_directory=os.path.isdir(src))
                except Exception:
                    pass

        lock_result = subprocess.run(
            [_find_uv(), "lock"], cwd=sandbox_dir,
            capture_output=True, text=True, timeout=120,
        )
        if lock_result.returncode != 0:
            combined = (lock_result.stdout or "") + "\n" + (lock_result.stderr or "")
            return {"status": "error", "message": f"Auto-dependency resolution failed (uv lock). Cannot commit.\n{combined[-500:]}"}

        sync_result = subprocess.run(
            [_find_uv(), "sync", "--frozen"], cwd=sandbox_dir,
            capture_output=True, text=True, timeout=300,
        )
        if sync_result.returncode != 0:
            combined = (sync_result.stdout or "") + "\n" + (sync_result.stderr or "")
            return {"status": "error", "message": f"Auto-dependency install failed (uv sync). Cannot commit.\n{combined[-500:]}"}

    staged_files = _collect_staged_files(sandbox_dir)
    staged_digest = _sandbox_digest(sandbox_dir)
    verified_digest = tool_context.state.get("evolution_verified_digest", "")
    if staged_files and verified_digest != staged_digest:
        return {
            "status": "error",
            "message": (
                "Commit blocked: staged files do not match a successful pytest "
                "verification. Run evolution_verify_sandbox(check='pytest') after "
                "the latest staged change, then commit."
            ),
        }

    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_repo = os.environ.get("GITHUB_REPO", "")
    use_remote = bool(github_token and github_repo)

    if use_remote:
        result = _evolution_commit_remote(staged_files, delete_files, commit_message, skip_local_update)
    else:
        result = _evolution_commit_local(staged_files, delete_files, commit_message, skip_local_update)

    if result["status"] != "success":
        _evolution_audit(
            "commit", _audit_actor(tool_context), "fail",
            files=[rel for _, rel in staged_files],
            digest=staged_digest,
            error=result.get("message", "unknown"),
            mode=result.get("mode", "unknown"),
        )
        return result

    # Clean up sandbox + cycle marker (disk-backed, see helpers at top of module)
    _sandbox_cycle_end(sandbox_dir)

    summary = []
    if staged_files:
        summary.append(f"added/updated {len(staged_files)} file(s)")
    if delete_files:
        summary.append(f"deleted {len(delete_files)} file(s)")

    mode = result.get("mode", "unknown")
    msg = f"Successfully {' and '.join(summary)} via {'remote push' if mode == 'remote' else 'local branch-merge'}."

    _evolution_audit(
        "commit", _audit_actor(tool_context), "ok",
        files=[rel for _, rel in staged_files],
        deleted=list(delete_files or []),
        digest=staged_digest,
        mode=mode,
        message=commit_message[:200],
    )

    # Auto-trigger reboot after successful commit — this is the final step of
    # the evolution cycle. The commit approval covers the reboot; no second
    # approval is needed. The transport layer will pick up the signal after
    # delivering this response and do a clean sys.exit(0).
    from app.tools.system import _write_exit_signal, EXIT_CODE_UPDATE
    _write_exit_signal(EXIT_CODE_UPDATE)
    msg += " Reboot signal dispatched — the system will shut down cleanly after this response."

    return {
        "status": "success",
        "message": msg,
    }


def evolution_git_pull(tool_context: ToolContext) -> dict:
    """Pulls the latest code from the GitHub remote repository into the current container and restarts.
    
    Use this when you want to fetch fresh code pushed by human administrators or other agents.
    
    Returns:
        dict: Status of the pull operation.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_repo = os.environ.get("GITHUB_REPO", "")
    if not github_token or not github_repo:
        # Fallback to standard git pull if it's a public repo or host has auth
        pull_url = "origin"
    else:
        pull_url = f"https://x-access-token:{github_token}@github.com/{github_repo}.git"

    try:
        subprocess.run(["git", "config", "pull.rebase", "false"], cwd=PROJECT_ROOT, check=True)
        result = subprocess.run(
            ["git", "pull", pull_url, "master"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout)[-500:].replace(github_token, "***")
            return {"status": "error", "message": f"Git pull failed: {err_msg}"}
            
        return {"status": "success", "message": f"Successfully pulled latest code:\n{result.stdout}\nRun system update (exit 100) to apply."}
    except Exception as e:
        err_msg = str(e).replace(github_token, "***")
        return {"status": "error", "message": f"Error during git pull: {err_msg}"}


def evolution_git_reset(tool_context: ToolContext) -> dict:
    """Resets the local workspace to match the last commit, deleting untracked 'dangling' files.
    
    Use this to clean up your workspace if you got stuck with leftover artifacts, 
    merge conflicts, or uncommitted files that prevent you from working.
    
    Returns:
        dict: Status of the reset operation.
    """
    try:
        # First clean untracked files (ignored files like /data/ are safe due to .gitignore)
        clean_res = subprocess.run(["git", "clean", "-fd"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True)
        # Then reset tracked files
        reset_res = subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True)
        
        return {
            "status": "success", 
            "message": f"Workspace reset successfully.\nClean output: {clean_res.stdout.strip()}\nReset output: {reset_res.stdout.strip()}"
        }
    except Exception as e:
        return {"status": "error", "message": f"Error during git reset: {e!s}"}


def evolution_sync_local_to_upstream(tool_context: ToolContext) -> dict:
    """Connects a detached local workspace to a remote GitHub repository and populates it.
    
    Use this on a first-time start when the agent lives in a fresh/empty repository
    or was cloned without its .git history.
    
    Returns:
        dict: Status of the synchronization.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_repo = os.environ.get("GITHUB_REPO", "")
    if not github_token or not github_repo:
        return {"status": "error", "message": "GITHUB_TOKEN and GITHUB_REPO must be configured first."}

    push_url = f"https://x-access-token:{github_token}@github.com/{github_repo}.git"

    try:
        # 1. Initialize git if not already present
        if not os.path.exists(os.path.join(PROJECT_ROOT, ".git")):
            subprocess.run(["git", "init"], cwd=PROJECT_ROOT, check=True)
        
        # 2. Configure remote 'origin'
        # Check if origin already exists
        remotes = subprocess.run(["git", "remote"], cwd=PROJECT_ROOT, capture_output=True, text=True).stdout
        if "origin" in remotes:
            subprocess.run(["git", "remote", "set-url", "origin", push_url], cwd=PROJECT_ROOT, check=True)
        else:
            subprocess.run(["git", "remote", "add", "origin", push_url], cwd=PROJECT_ROOT, check=True)

        # 3. Identity configuration
        bot_name = os.environ.get("BOT_NAME", "Ori")
        subprocess.run(["git", "config", "user.email", "agent@evolution.local"], cwd=PROJECT_ROOT, check=True)
        subprocess.run(["git", "config", "user.name", f"{bot_name} (Agent)"], cwd=PROJECT_ROOT, check=True)

        # 4. Populate repository
        subprocess.run(["git", "add", "."], cwd=PROJECT_ROOT, check=True)
        # Try to commit, but ignore if nothing changed
        try:
            subprocess.run(["git", "commit", "-m", f"Initial synchronization by {bot_name}"], cwd=PROJECT_ROOT, check=True)
        except subprocess.CalledProcessError:
            pass # No changes to commit
        
        # 5. Push to master
        result = subprocess.run(["git", "push", "-u", "origin", "master"], cwd=PROJECT_ROOT, capture_output=True, text=True)
        
        if result.returncode != 0:
             return {"status": "error", "message": f"Git push failed: {result.stderr.replace(github_token, '***')}"}

        return {"status": "success", "message": f"Workspace successfully connected and pushed to {github_repo}."}
        
    except Exception as e:
        return {"status": "error", "message": f"Synchronization failed: {str(e).replace(github_token, '***')}"}



def evolution_git_fetch(remote: str = "origin", tool_context: ToolContext = None) -> dict:
    """Fetches latest refs from a remote without merging anything.

    Use this to see what's new on origin or upstream before deciding to pull/merge.

    Args:
        remote (str): Remote name to fetch from (e.g. 'origin', 'upstream'). Default: 'origin'.

    Returns:
        dict: Fetch result and list of updated refs.
    """
    try:
        result = subprocess.run(
            ["git", "fetch", remote],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return {"status": "error", "message": f"Fetch failed: {(result.stderr or result.stdout)[-500:]}"}

        return {"status": "success", "message": f"Fetched from {remote}.", "output": (result.stderr or "").strip()[-500:]}
    except Exception as e:
        return {"status": "error", "message": f"Fetch error: {e}"}


def evolution_git_log(ref: str = "HEAD", count: int = 10, tool_context: ToolContext = None) -> dict:
    """Shows compact commit history for a branch or ref.

    Args:
        ref (str): Branch, tag, or commit ref (e.g. 'HEAD', 'origin/master', 'upstream/master'). Default: 'HEAD'.
        count (int): Number of commits to show. Default: 10. Max: 50.

    Returns:
        dict: List of commits with hash, date, author, and subject.
    """
    count = min(max(count, 1), 50)
    try:
        result = subprocess.run(
            ["git", "log", ref, f"-{count}", "--format=%H|%ad|%an|%s", "--date=short"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {"status": "error", "message": f"Git log failed: {(result.stderr or result.stdout)[-500:]}"}

        commits = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({"hash": parts[0][:8], "date": parts[1], "author": parts[2], "subject": parts[3]})

        return {"status": "success", "ref": ref, "commits": commits}
    except Exception as e:
        return {"status": "error", "message": f"Git log error: {e}"}


def evolution_git_diff_summary(base: str, head: str = "HEAD", tool_context: ToolContext = None) -> dict:
    """Shows a compact summary of changes between two refs (files changed, insertions, deletions).

    Use this to quickly see what changed between branches without reading full diffs.

    Args:
        base (str): Base ref to compare from (e.g. 'origin/master', 'upstream/master', 'HEAD~5').
        head (str): Head ref to compare to. Default: 'HEAD'.

    Returns:
        dict: Summary with file stats and total counts.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", f"{base}...{head}"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            # Try two-dot diff as fallback (for unrelated histories)
            result = subprocess.run(
                ["git", "diff", "--stat", f"{base}..{head}"],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
            )

        if result.returncode != 0:
            return {"status": "error", "message": f"Diff failed: {(result.stderr or result.stdout)[-500:]}"}

        lines = result.stdout.strip().splitlines()
        summary_line = lines[-1] if lines else "No changes"

        files = []
        for line in lines[:-1]:
            line = line.strip()
            if line and "|" in line:
                parts = line.split("|", 1)
                files.append({"file": parts[0].strip(), "changes": parts[1].strip()})

        return {"status": "success", "base": base, "head": head, "summary": summary_line, "files": files}
    except Exception as e:
        return {"status": "error", "message": f"Diff error: {e}"}


def evolution_git_diff_file(file_path: str, base: str, head: str = "HEAD", tool_context: ToolContext = None) -> dict:
    """Shows the actual diff for a specific file between two refs.

    Use this after evolution_git_diff_summary to inspect individual file changes.

    Args:
        file_path (str): Path to the file to diff (e.g. 'app/tools/scheduling.py').
        base (str): Base ref (e.g. 'origin/master', 'upstream/master').
        head (str): Head ref. Default: 'HEAD'.

    Returns:
        dict: The diff content for the specified file.
    """
    try:
        result = subprocess.run(
            ["git", "diff", f"{base}...{head}", "--", file_path],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["git", "diff", f"{base}..{head}", "--", file_path],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
            )

        if result.returncode != 0:
            return {"status": "error", "message": f"Diff failed: {(result.stderr or result.stdout)[-500:]}"}

        diff_text = result.stdout.strip()
        if not diff_text:
            return {"status": "success", "message": f"No changes to {file_path} between {base} and {head}."}

        # Truncate very large diffs
        if len(diff_text) > 3000:
            diff_text = diff_text[:3000] + "\n... [truncated, use evolution_read_file for full content]"

        return {"status": "success", "file": file_path, "base": base, "head": head, "diff": diff_text}
    except Exception as e:
        return {"status": "error", "message": f"Diff error: {e}"}


def evolution_git_branches(tool_context: ToolContext = None) -> dict:
    """Lists all local and remote branches with their latest commit.

    Returns:
        dict: List of branches with ref, last commit hash, and subject.
    """
    try:
        result = subprocess.run(
            ["git", "branch", "-a", "--format=%(refname:short)|%(objectname:short)|%(subject)"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return {"status": "error", "message": f"Branch list failed: {(result.stderr or result.stdout)[-500:]}"}

        branches = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) >= 2:
                branches.append({"branch": parts[0], "hash": parts[1], "subject": parts[2] if len(parts) > 2 else ""})

        return {"status": "success", "branches": branches}
    except Exception as e:
        return {"status": "error", "message": f"Branch list error: {e}"}
