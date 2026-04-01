"""Tools for researching bugs, library docs, and package versions."""

import logging
import subprocess
import sys

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


def check_installed_package(package_name: str, tool_context: ToolContext) -> dict:
    """Checks the installed version and metadata of a Python package.

    Use this to verify which version of a library is actually installed before
    writing code against it. This prevents coding against the wrong API version.

    Args:
        package_name (str): The pip package name (e.g., 'google-adk', 'flask', 'httpx').

    Returns:
        dict: Package version, location, dependencies, and summary.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", package_name],
            capture_output=True, text=True, timeout=15,
        )

        if result.returncode != 0:
            return {
                "status": "error",
                "message": f"Package '{package_name}' is not installed.",
            }

        # Parse pip show output into a dict
        info = {}
        for line in result.stdout.strip().splitlines():
            if ": " in line:
                key, _, value = line.partition(": ")
                info[key.strip()] = value.strip()

        return {
            "status": "success",
            "package": package_name,
            "version": info.get("Version", "unknown"),
            "summary": info.get("Summary", ""),
            "location": info.get("Location", ""),
            "requires": info.get("Requires", ""),
            "required_by": info.get("Required-by", ""),
        }

    except Exception as e:
        return {"status": "error", "message": f"Failed to check package: {e}"}
