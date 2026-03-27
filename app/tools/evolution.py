import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime

from google.adk.auth.auth_credential import AuthCredential, OAuth2Auth
from google.adk.auth.auth_schemes import OAuth2, OAuthGrantType
from google.adk.auth.auth_tool import AuthConfig
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



def evolution_stage_change(
    file_path: str, new_content: str, tool_context: ToolContext
) -> dict:
    """Stages a code change in a protected sandbox environment.

    This does NOT modify the live running code. It prepares the change for verification.

    Args:
        file_path (str): The relative path to the file to modify.
        new_content (str): The full new content of the file.

    Returns:
        dict: Status of the staging operation.
    """
    if file_path.endswith(".env"):
        return {"status": "error", "message": "Security error: Writing to .env directly is blocked. Instruct human to configure integrations properly."}
    
    sandbox_dir = os.path.abspath("./data/sandbox")
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

            # SECURE: Uses py_compile module to check syntax without string interpolation
            # Note: This tool was updated by the user to be more robust.
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", resolved],
                capture_output=True, text=True, timeout=15,
            )

        elif check == "import":
            if not target:
                return {"status": "error", "message": "Module name required for import check."}

            # SECURE: Passes target as sys.argv[1] to avoid code injection
            check_script = "import sys, importlib; importlib.import_module(sys.argv[1]); print('Import OK')"
            result = subprocess.run(
                [sys.executable, "-c", check_script, target],
                cwd=sandbox_dir,
                capture_output=True, text=True, timeout=15,
            )

        elif check == "pytest":
            # Auto-bootstrap: symlink project config and backfill existing tests
            # so the sandbox can resolve dependencies and run the full test suite.
            for config_file in ("pyproject.toml", "uv.lock"):
                src = os.path.join(PROJECT_ROOT, config_file)
                dst = os.path.join(sandbox_dir, config_file)
                if os.path.exists(src) and not os.path.exists(dst):
                    os.symlink(src, dst)

            # Backfill existing test files that weren't staged (e.g. conftest.py)
            live_tests = os.path.join(PROJECT_ROOT, "tests")
            sandbox_tests = os.path.join(sandbox_dir, "tests")
            if os.path.isdir(live_tests):
                os.makedirs(sandbox_tests, exist_ok=True)
                for fname in os.listdir(live_tests):
                    if fname.startswith("__"):
                        continue
                    src = os.path.join(live_tests, fname)
                    dst = os.path.join(sandbox_tests, fname)
                    if os.path.isfile(src) and not os.path.exists(dst):
                        os.symlink(src, dst)

            result = subprocess.run(
                ["uv", "run", "pytest", "tests"],
                cwd=sandbox_dir,
                capture_output=True, text=True, timeout=120,
            )

        else:
            return {"status": "error", "message": f"Unknown check type: '{check}'. Use 'syntax', 'pytest', or 'import'."}

        if result.returncode == 0:
            return {
                "status": "success",
                "message": f"Verification PASSED ({check}).",
                "output": result.stdout[-500:] if result.stdout else "",
            }
        else:
            return {
                "status": "error",
                "message": f"Verification FAILED ({check}).",
                "output": (result.stderr or result.stdout)[-500:],
            }
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": f"Verification timed out ({check})."}
    except Exception as e:
        return {"status": "error", "message": f"Verification crashed: {e}"}



def evolution_commit_and_push(commit_message: str, tool_context: ToolContext) -> dict:
    """Commits and pushes the verified changes from the sandbox to the GitHub repository.

    ONLY call this after ALL verification checks pass.

    Args:
        commit_message (str): A descriptive message explaining the improvement.

    Returns:
        dict: Status of the commit and push operation.
    """
    sandbox_dir = os.path.abspath("./data/sandbox")
    if not os.path.exists(sandbox_dir):
        return {"status": "error", "message": "No staged changes found in sandbox."}

    # Validate GitHub credentials early
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
            "message": f"Missing credentials: {', '.join(missing)}. "
                       f"Report this to the user and use `configure_integration` to collect each key securely. "
                       f"Do NOT ask the user to paste credentials directly in chat.",
        }

    # Collect sandbox files to copy
    staged_files = []
    for root, _dirs, files in os.walk(sandbox_dir):
        if "__pycache__" in root:
            continue
        for fname in files:
            src = os.path.join(root, fname)
            rel = os.path.relpath(src, sandbox_dir)
            staged_files.append((src, rel))

    if not staged_files:
        return {"status": "error", "message": "Sandbox is empty — nothing to commit."}

    # Use a temporary directory for the git operations to bypass potential permission
    # issues with root-owned .git mounts in the main /code directory.
    tmp_repo_dir = f"/tmp/evolution_{uuid.uuid4().hex[:8]}"
    push_url = f"https://x-access-token:{github_token}@github.com/{github_repo}.git"

    try:
        # 1. Clone the repo (shallow) to the temporary directory
        subprocess.run(
            ["git", "clone", "--depth", "1", push_url, tmp_repo_dir],
            capture_output=True, text=True, check=True, timeout=60,
        )

        # 2. Copy the staged changes into the temporary clone
        for src, rel in staged_files:
            dst = os.path.join(tmp_repo_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)

        # 3. Commit and Push from the temporary clone
        subprocess.run(["git", "config", "user.email", "agent@evolution.local"], cwd=tmp_repo_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Agent Evolution"], cwd=tmp_repo_dir, check=True)

        rel_paths = [rel for _, rel in staged_files]
        subprocess.run(["git", "add"] + rel_paths, cwd=tmp_repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", commit_message], cwd=tmp_repo_dir, check=True)

        result = subprocess.run(
            ["git", "push", "origin", "HEAD:master"],
            cwd=tmp_repo_dir, capture_output=True, text=True, timeout=60,
        )

        if result.returncode != 0:
            err_msg = (result.stderr or result.stdout)[-500:].replace(github_token, "***")
            return {"status": "error", "message": f"git push failed: {err_msg}"}

        # 4. Push succeeded — now apply changes to live code for partial instant effect
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

    # Clean up sandbox
    shutil.rmtree(sandbox_dir, ignore_errors=True)

    return {
        "status": "success",
        "message": f"Committed and pushed {len(staged_files)} file(s) via temporary clone: {', '.join(rel_paths)}",
    }



