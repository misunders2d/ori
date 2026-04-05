"""Tests for the 'security' check in evolution_verify_sandbox."""
import os
import subprocess
import tempfile
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sandbox(tmp: str) -> str:
    """Create a minimal sandbox directory with a .venv so the check passes pre-flight."""
    sandbox = os.path.join(tmp, "data", "sandbox")
    os.makedirs(os.path.join(sandbox, ".venv", "bin"), exist_ok=True)
    # Create a dummy uv.lock so export can "work"
    with open(os.path.join(sandbox, "uv.lock"), "w") as f:
        f.write("")
    return sandbox


def _fake_run_factory(export_stdout="somepackage==1.0.0\n", audit_rc=0, audit_stdout="", audit_stderr=""):
    """Return a side_effect function for subprocess.run that handles uv export + pip-audit."""
    def _side_effect(cmd, **kwargs):
        prog = cmd[0] if cmd else ""
        # Detect uv export
        if "uv" in prog or (len(cmd) > 1 and cmd[1] == "export"):
            result = MagicMock()
            result.returncode = 0
            result.stdout = export_stdout
            result.stderr = ""
            return result
        # Detect pip_audit
        if "pip_audit" in " ".join(cmd) or "-m" in cmd:
            result = MagicMock()
            result.returncode = audit_rc
            result.stdout = audit_stdout
            result.stderr = audit_stderr
            return result
        # Fallback
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result
    return _side_effect


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSecurityCheck:

    def test_no_venv_returns_error(self, tmp_path):
        """Security check fails gracefully when sandbox has no .venv."""
        sandbox = os.path.join(str(tmp_path), "data", "sandbox")
        os.makedirs(sandbox, exist_ok=True)
        # No .venv directory

        with patch("app.tools.evolution.os.path.abspath", return_value=sandbox):
            # Import after patch so PROJECT_ROOT doesn't interfere
            from app.tools.evolution import evolution_verify_sandbox
            ctx = MagicMock()
            ctx.state = {"evolution_cycle_active": True}
            result = evolution_verify_sandbox("security", ctx)

        assert result["status"] == "error"
        assert ".venv" in result["message"]

    def test_export_failure_returns_error(self, tmp_path):
        """If uv export fails, we get a clear error."""
        sandbox = _make_sandbox(str(tmp_path))

        def _export_fails(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "uv export failed: no lockfile"
            return result

        with (
            patch("app.tools.evolution.os.path.abspath", return_value=sandbox),
            patch("subprocess.run", side_effect=_export_fails),
        ):
            from app.tools.evolution import evolution_verify_sandbox
            ctx = MagicMock()
            ctx.state = {"evolution_cycle_active": True}
            result = evolution_verify_sandbox("security", ctx)

        assert result["status"] == "error"
        assert "export" in result["message"].lower() or "requirements" in result["message"].lower()

    def test_audit_pass(self, tmp_path):
        """Clean audit returns success."""
        sandbox = _make_sandbox(str(tmp_path))

        with (
            patch("app.tools.evolution.os.path.abspath", return_value=sandbox),
            patch("subprocess.run", side_effect=_fake_run_factory(
                audit_rc=0,
                audit_stdout="No known vulnerabilities found",
            )),
        ):
            from app.tools.evolution import evolution_verify_sandbox
            ctx = MagicMock()
            ctx.state = {"evolution_cycle_active": True}
            result = evolution_verify_sandbox("security", ctx)

        assert result["status"] == "success"
        assert "PASSED" in result["message"]

    def test_audit_fail_with_cves(self, tmp_path):
        """Vulnerable packages cause a failure with details."""
        sandbox = _make_sandbox(str(tmp_path))
        vuln_output = (
            "Name       Version ID             Fix Versions\n"
            "---------- ------- -------------- ------------\n"
            "cryptography 41.0.0 GHSA-xxx-yyy  41.0.2\n"
        )

        with (
            patch("app.tools.evolution.os.path.abspath", return_value=sandbox),
            patch("subprocess.run", side_effect=_fake_run_factory(
                audit_rc=1,
                audit_stdout=vuln_output,
            )),
        ):
            from app.tools.evolution import evolution_verify_sandbox
            ctx = MagicMock()
            ctx.state = {"evolution_cycle_active": True}
            result = evolution_verify_sandbox("security", ctx)

        assert result["status"] == "error"
        assert "FAILED" in result["message"]
        assert "cryptography" in result["output"]

    def test_reqs_file_cleaned_up(self, tmp_path):
        """The temporary .audit-requirements.txt is removed after the check."""
        sandbox = _make_sandbox(str(tmp_path))

        with (
            patch("app.tools.evolution.os.path.abspath", return_value=sandbox),
            patch("subprocess.run", side_effect=_fake_run_factory(audit_rc=0)),
        ):
            from app.tools.evolution import evolution_verify_sandbox
            ctx = MagicMock()
            ctx.state = {"evolution_cycle_active": True}
            evolution_verify_sandbox("security", ctx)

        assert not os.path.exists(os.path.join(sandbox, ".audit-requirements.txt"))

    def test_unknown_check_lists_security(self, tmp_path):
        """The 'security' check type is mentioned in the unknown-check error."""
        sandbox = os.path.join(str(tmp_path), "data", "sandbox")
        os.makedirs(sandbox, exist_ok=True)

        from app.tools.evolution import evolution_verify_sandbox
        ctx = MagicMock()
        ctx.state = {"evolution_cycle_active": True}

        with patch("app.tools.evolution.os.path.abspath", return_value=sandbox):
            result = evolution_verify_sandbox("bogus", ctx)

        assert "security" in result["message"]
