import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from typing import List, Optional

from google.adk.tools.tool_context import ToolContext


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

def _safe_resolve_path(file_path: str, base_dir: str) -> str | None:
    """Resolve file_path relative to base_dir and ensure it stays within it."""
    base = os.path.abspath(base_dir)
    resolved = os.path.abspath(os.path.join(base, file_path))
    if not resolved.startswith(base + os.sep) and resolved != base:
        return None
    return resolved



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

    # Infrastructure files are outside the self-evolution scope.
    # Recovery and deployment logic must remain untouched to prevent bricking.
    INFRA_FILES = {"Dockerfile", "docker-compose.yml", "entrypoint.sh", "launcher.sh", "install.sh"}
    if os.path.basename(file_path) in INFRA_FILES:
        return {"status": "error", "message": f"Infrastructure file '{file_path}' is protected. Deployment and recovery scripts cannot be modified by self-evolution."}

    sandbox_dir = os.path.abspath("./data/sandbox")

    # Clear stale sandbox from previous (possibly rejected) evolution cycles
    if os.path.exists(sandbox_dir):
        state = tool_context.state
        if not state.get("evolution_cycle_active"):
            shutil.rmtree(sandbox_dir, ignore_errors=True)
            state["evolution_cycle_active"] = True

    os.makedirs(sandbox_dir, exist_ok=True)

    resolved = _safe_resolve_path(file_path, sandbox_dir)
    if resolved is None:
        return {"status": "error", "message": "Path traversal denied. Use relative paths within the sandbox."}

    os.makedirs(os.path.dirname(resolved), exist_ok=True)

    try:
        with open(resolved, "w") as f:
            f.write(new_content)
        return {
            "status": "success",
            "message": f"Staged changes for {file_path} in sandbox.",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}



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
            # Resolve dependencies when pyproject.toml is staged.
            # Generates updated uv.lock so the Docker build (uv sync --frozen) succeeds.
            staged_pyproject = os.path.join(sandbox_dir, "pyproject.toml")
            if not os.path.exists(staged_pyproject):
                return {"status": "error", "message": "No pyproject.toml staged. Stage it first, then run 'deps' check."}

            # uv lock needs the full project context — symlink everything else
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

            result = subprocess.run(
                ["uv", "lock"],
                cwd=sandbox_dir,
                capture_output=True, text=True, timeout=120,
            )

            if result.returncode == 0:
                # Copy the generated uv.lock back into the sandbox as a staged file
                generated_lock = os.path.join(sandbox_dir, "uv.lock")
                if os.path.exists(generated_lock) and not os.path.islink(generated_lock):
                    return {
                        "status": "success",
                        "message": "Dependencies resolved. uv.lock updated and staged.",
                        "output": result.stdout[-500:] if result.stdout else "",
                    }
                return {
                    "status": "success",
                    "message": "Dependencies resolved (uv.lock unchanged).",
                    "output": result.stdout[-500:] if result.stdout else "",
                }
            else:
                combined = (result.stdout or "") + "\n" + (result.stderr or "")
                return {
                    "status": "error",
                    "message": "Dependency resolution FAILED.",
                    "output": combined[-1000:],
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

                pytest_script = (
                    "import pytest, sys, os; "
                    "os.environ['PYTHONPATH'] = os.getcwd(); "
                    "sys.exit(pytest.main(['tests', '-v']))"
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
            return {
                "status": "success",
                "message": f"Verification PASSED ({check}).",
                "output": result.stdout[-500:] if result.stdout else "",
            }
        else:
            combined = (result.stdout or "") + "\n" + (result.stderr or "")
            return {
                "status": "error",
                "message": f"Verification FAILED ({check}).",
                "output": combined[-1000:],
            }
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": f"Verification timed out ({check})."}
    except Exception as e:
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
    """Commit changes locally using branch-test-merge workflow (no remote needed).

    Flow: create feature branch -> apply changes -> commit -> merge to master.
    If anything fails, the branch is deleted and master is untouched.
    """
    bot_name = os.environ.get("BOT_NAME", "Ori")
    branch_name = f"evo/{uuid.uuid4().hex[:8]}"
    signed_message = _make_signed_message(commit_message)

    try:
        # Create and switch to feature branch
        subprocess.run(["git", "checkout", "-b", branch_name], cwd=PROJECT_ROOT, check=True,
                        capture_output=True, text=True)

        # Apply staged files to the working tree
        for src, rel in staged_files:
            dst = os.path.join(PROJECT_ROOT, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

        # Apply deletions
        if delete_files:
            for rel_path in delete_files:
                live_target = os.path.join(PROJECT_ROOT, rel_path)
                if os.path.exists(live_target):
                    os.remove(live_target)
                subprocess.run(["git", "rm", "-f", "--ignore-unmatch", rel_path],
                                cwd=PROJECT_ROOT, capture_output=True, text=True)

        # Stage and commit
        subprocess.run(["git", "add", "-A"], cwd=PROJECT_ROOT, check=True,
                        capture_output=True, text=True)
        subprocess.run(
            ["git", "config", "user.email", "agent@evolution.local"],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", f"{bot_name} (Agent)"],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
        subprocess.run(["git", "commit", "-m", signed_message], cwd=PROJECT_ROOT, check=True,
                        capture_output=True, text=True)

        # Merge back to master
        subprocess.run(["git", "checkout", "master"], cwd=PROJECT_ROOT, check=True,
                        capture_output=True, text=True)
        result = subprocess.run(
            ["git", "merge", "--no-ff", branch_name, "-m", f"merge: {signed_message}"],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )

        if result.returncode != 0:
            # Merge conflict — abort and clean up
            subprocess.run(["git", "merge", "--abort"], cwd=PROJECT_ROOT, capture_output=True)
            subprocess.run(["git", "branch", "-D", branch_name], cwd=PROJECT_ROOT, capture_output=True)
            return {"status": "error", "message": f"Merge conflict. Branch discarded.\n{result.stderr[-500:]}"}

        # Clean up the feature branch
        subprocess.run(["git", "branch", "-d", branch_name], cwd=PROJECT_ROOT, capture_output=True)

    except subprocess.CalledProcessError as e:
        # Ensure we're back on master and clean up
        subprocess.run(["git", "checkout", "master"], cwd=PROJECT_ROOT, capture_output=True)
        subprocess.run(["git", "branch", "-D", branch_name], cwd=PROJECT_ROOT, capture_output=True)
        err_msg = (e.stderr or e.stdout or str(e))[-500:]
        return {"status": "error", "message": f"Local evolution failed: {err_msg}"}
    except Exception as e:
        subprocess.run(["git", "checkout", "master"], cwd=PROJECT_ROOT, capture_output=True)
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

    IMPORTANT: You MUST NOT commit changes that remove, modify, or bypass system guardrails
    (event callbacks like `before_agent_callback`, `before_model_callback`, etc.)
    unless explicitly requested by the user.

    Args:
        commit_message (str): A descriptive message explaining the improvement.
        delete_files (Optional[List[str]]): List of relative paths to files that should be deleted.
        skip_local_update (bool): If True (remote mode only), pushes but does NOT update the local filesystem.
            A hard reboot (exit 100) will be required afterward to apply changes.
    """
    sandbox_dir = os.path.abspath("./data/sandbox")
    has_staged = os.path.exists(sandbox_dir) and any(
        os.path.isfile(os.path.join(root, f))
        for root, _, files in os.walk(sandbox_dir) for f in files
    )

    if not has_staged and not delete_files:
        return {"status": "error", "message": "Nothing to commit or delete."}

    staged_files = _collect_staged_files(sandbox_dir)

    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_repo = os.environ.get("GITHUB_REPO", "")
    use_remote = bool(github_token and github_repo)

    if use_remote:
        result = _evolution_commit_remote(staged_files, delete_files, commit_message, skip_local_update)
    else:
        result = _evolution_commit_local(staged_files, delete_files, commit_message, skip_local_update)

    if result["status"] != "success":
        return result

    # Clean up sandbox
    if os.path.exists(sandbox_dir):
        shutil.rmtree(sandbox_dir, ignore_errors=True)

    tool_context.state["evolution_cycle_active"] = False

    summary = []
    if staged_files:
        summary.append(f"added/updated {len(staged_files)} file(s)")
    if delete_files:
        summary.append(f"deleted {len(delete_files)} file(s)")

    mode = result.get("mode", "unknown")
    msg = f"Successfully {' and '.join(summary)} via {'remote push' if mode == 'remote' else 'local branch-merge'}."
    if skip_local_update and use_remote:
        msg += " Local update skipped. Scheduling automatic hard reboot (exit 100) to apply changes."
        from app.tools.system import _schedule_restart, EXIT_CODE_UPDATE
        _schedule_restart(EXIT_CODE_UPDATE)

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
