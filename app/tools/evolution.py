import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from typing import List, Optional

from google.adk.auth.auth_credential import AuthCredential, OAuth2Auth
from google.adk.auth.auth_schemes import OAuth2, OAuthGrantType
from google.adk.auth.auth_tool import AuthConfig
from google.adk.tools.tool_context import ToolContext


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SANDBOX_BASE = os.path.join(os.path.abspath("./data"), "sandbox")


def _get_sandbox_dir(tool_context: "ToolContext") -> str:
    """Return a per-session sandbox directory to prevent concurrent clobbering."""
    session = getattr(tool_context, "session", None)
    sid = str(getattr(session, "id", "")) if session else ""
    # Sanitise session id to be filesystem-safe
    safe_sid = sid.replace("/", "_").replace("..", "_").strip("_") if sid else "default"
    return os.path.join(_SANDBOX_BASE, safe_sid)


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



def evolution_read_sandbox_file(file_path: str, tool_context: ToolContext) -> dict:
    """Reads the content of a file from the sandbox environment.

    Use this to review changes staged by yourself or another agent before verification.

    Args:
        file_path (str): The relative path to the file within the sandbox.

    Returns:
        dict: Content of the file or error.
    """
    sandbox_dir = _get_sandbox_dir(tool_context)
    resolved = _safe_resolve_path(file_path, sandbox_dir)
    if resolved is None:
        return {"status": "error", "message": "Path traversal denied."}
    
    try:
        if not os.path.exists(resolved):
            return {"status": "error", "message": f"File {file_path} not found in sandbox."}
        with open(resolved) as f:
            return {"status": "success", "content": f.read()}
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

    sandbox_dir = _get_sandbox_dir(tool_context)
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
            - 'pytest' — Run the full test suite.
            - 'import' — Try importing a module (requires target, e.g., 'app.tools').
        target (str): The file path (for 'syntax') or module name (for 'import'). Not needed for 'pytest'.

    Returns:
        dict: Verification status and output.
    """
    sandbox_dir = _get_sandbox_dir(tool_context)
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

        elif check == "import":
            if not target:
                return {"status": "error", "message": "Module name required for import check."}

            check_script = "import sys, importlib; importlib.import_module(sys.argv[1]); print('Import OK')"
            result = subprocess.run(
                [sys.executable, "-c", check_script, target],
                cwd=sandbox_dir,
                capture_output=True, text=True, timeout=15,
            )

        elif check == "pytest":
            # Auto-bootstrap: symlink project config and backfill existing tests
            for config_file in ("pyproject.toml", "uv.lock"):
                src = os.path.join(PROJECT_ROOT, config_file)
                dst = os.path.join(sandbox_dir, config_file)
                if os.path.exists(src) and not os.path.exists(dst):
                    os.symlink(src, dst)

            live_tests = os.path.join(PROJECT_ROOT, "tests")
            sandbox_tests = os.path.join(sandbox_dir, "tests")
            if os.path.isdir(live_tests):
                os.makedirs(sandbox_tests, exist_ok=True)
                for fname in os.listdir(live_tests):
                    # SECURE: ONLY symlink actual test files, skipping dot-folders or __pycache__
                    if fname.startswith("."):
                        continue
                    src = os.path.join(live_tests, fname)
                    dst = os.path.join(sandbox_tests, fname)
                    if os.path.isfile(src) and not os.path.exists(dst):
                        os.symlink(src, dst)

            # Use a Python wrapper to run pytest and ensure clean output capture
            # We explicitly set PYTHONPATH to include the sandbox_dir
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
            for config_file in ("pyproject.toml", "uv.lock"):
                link = os.path.join(sandbox_dir, config_file)
                if os.path.islink(link):
                    os.unlink(link)
            if os.path.isdir(sandbox_tests):
                for fname in os.listdir(sandbox_tests):
                    link = os.path.join(sandbox_tests, fname)
                    if os.path.islink(link):
                        os.unlink(link)

        else:
            return {"status": "error", "message": f"Unknown check type: '{check}'. Use 'syntax', 'pytest', or 'import'."}

        if result.returncode == 0:
            return {
                "status": "success",
                "message": f"Verification PASSED ({check}).",
                "output": result.stdout[-500:] if result.stdout else "",
            }
        else:
            # Combined capture to find the actual pytest failure
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



def evolution_commit_and_push(
    commit_message: str, tool_context: ToolContext, delete_files: Optional[List[str]] = None
) -> dict:
    """Commits and pushes verified changes and handles deletions in the GitHub repository.

    ONLY call this after ALL verification checks pass.

    IMPORTANT: You MUST NOT commit changes that remove, modify, or bypass system guardrails
    (event callbacks like `before_agent_callback`, `before_model_callback`, etc.)
    unless explicitly requested by the user.

    Args:
        commit_message (str): A descriptive message explaining the improvement.
        delete_files (Optional[List[str]]): List of relative paths to files that should be deleted.

    Returns:
        dict: Status of the commit and push operation.
    """
    sandbox_dir = _get_sandbox_dir(tool_context)
    # If no staged files AND no deletions, return error
    has_staged = os.path.exists(sandbox_dir) and any(os.path.isfile(os.path.join(root, f)) for root, _, files in os.walk(sandbox_dir) for f in files)
    
    if not has_staged and not delete_files:
        return {"status": "error", "message": "Nothing to commit or delete."}

    github_token = os.environ.get("GITHUB_TOKEN", "")
    github_repo = os.environ.get("GITHUB_REPO", "")
    if not github_token or not github_repo:
        missing = []
        if not github_token:
            missing.append("GITHUB_TOKEN")
        if not github_repo:
            missing.append("GITHUB_REPO")
        return {
            "status": "auth_required",
            "message": f"Missing credentials: {', '.join(missing)}.",
        }

    staged_files = []
    if os.path.exists(sandbox_dir):
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
                staged_files.append((src, rel))

    tmp_repo_dir = f"/tmp/evolution_{uuid.uuid4().hex[:8]}"
    push_url = f"https://x-access-token:{github_token}@github.com/{github_repo}.git"

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

        bot_name = os.environ.get("BOT_NAME", "Ori")
        subprocess.run(["git", "config", "user.email", "agent@evolution.local"], cwd=tmp_repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", f"{bot_name} (Agent)"], cwd=tmp_repo_dir, check=True)

        if staged_files:
            rel_paths = [rel for _, rel in staged_files]
            chunk_size = 50
            for i in range(0, len(rel_paths), chunk_size):
                subprocess.run(["git", "add"] + rel_paths[i:i+chunk_size], cwd=tmp_repo_dir, check=True)
            
        # Append "evolved by {bot_name}" to the commit message
        signed_message = f"{commit_message}\n\nevolved by {bot_name}"
        subprocess.run(["git", "commit", "-m", signed_message], cwd=tmp_repo_dir, check=True)

        result = subprocess.run(
            ["git", "push", "origin", "HEAD:master"],
            cwd=tmp_repo_dir, capture_output=True, text=True, timeout=60,
        )

        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout)[-500:].replace(github_token, "***")
            return {"status": "error", "message": f"git push failed: {err_msg}"}

        if delete_files:
            for rel_path in delete_files:
                live_target = os.path.join(PROJECT_ROOT, rel_path)
                if os.path.exists(live_target):
                    os.remove(live_target)

        for src, rel in staged_files:
            live_dst = os.path.join(PROJECT_ROOT, rel)
            os.makedirs(os.path.dirname(live_dst), exist_ok=True)
            shutil.copy2(src, live_dst)

    except subprocess.CalledProcessError as e:
        err_msg = (e.stderr or e.stdout or str(e))[-500:].replace(github_token, "***")
        return {"status": "error", "message": f"Git error: {err_msg}"}
    except Exception as e:
        return {"status": "error", "message": f"Error during push: {e!s}"}
    finally:
        if os.path.exists(tmp_repo_dir):
            shutil.rmtree(tmp_repo_dir)

    if os.path.exists(sandbox_dir):
        shutil.rmtree(sandbox_dir, ignore_errors=True)

    summary = []
    if staged_files:
        summary.append(f"added/updated {len(staged_files)} file(s)")
    if delete_files:
        summary.append(f"deleted {len(delete_files)} file(s)")

    return {
        "status": "success",
        "message": f"Successfully {' and '.join(summary)} via temporary clone.",
    }
