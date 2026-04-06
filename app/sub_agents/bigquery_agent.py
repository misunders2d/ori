"""BigQuery sub-agent with per-table access control.

Access control uses email-based authorization lists from table metadata.
The user's email is injected into session state by the Slack poller
(state key: 'user_email'). Admin users (from ADMIN_USER_IDS) bypass
all table restrictions.
"""

import logging
import os
import re
from typing import Any

from google.adk.agents import Agent
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

from app.app_utils.models import get_model
from app.callbacks.guardrails import prompt_injection_guardrail
from app.tools.bigquery_data import get_table_data, table_data
from app.tools.bigquery_tools import create_bigquery_toolset
from app.toolsets import ScratchpadToolset

logger = logging.getLogger(__name__)


def _get_admin_emails() -> list[str]:
    """Return admin user IDs from environment."""
    return [u.strip() for u in os.environ.get("ADMIN_USER_IDS", "").split(",") if u.strip()]


def before_bq_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict | None:
    """Per-table access control for BigQuery tools.

    Intercepts execute_sql and get_table_info calls, extracts table references,
    and checks the user's email against authorized_users lists in the table catalog.
    Admin users bypass all restrictions.
    """
    tool_name = getattr(tool, "name", "")

    # Only gate data-access tools
    if tool_name not in ("execute_sql", "get_table_info"):
        return None

    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    user_id = state.get("user_id", "")

    # Admin users bypass table-level restrictions
    admin_ids = _get_admin_emails()
    if user_id in admin_ids:
        return None

    # user_id is the email for Slack users, tg_XXX for Telegram
    user_email = user_id

    # Extract table references from SQL query
    tables_to_check = []
    query = args.get("query", "")
    if query:
        found_tables = re.findall(
            r"(?:FROM|JOIN)\s+`?([\w.-]+)`?", query, re.IGNORECASE
        )
        for table_name in found_tables:
            parts = table_name.split(".")
            if len(parts) == 3:
                tables_to_check.append({
                    "project_id": parts[0],
                    "dataset_id": parts[1],
                    "table_id": parts[2],
                })
            elif len(parts) == 2:
                tables_to_check.append({
                    "project_id": args.get("project_id", ""),
                    "dataset_id": parts[0],
                    "table_id": parts[1],
                })
    else:
        # Metadata lookup (get_table_info)
        project_id = args.get("project_id", "")
        dataset_id = args.get("dataset_id", "")
        table_id = args.get("table_id", "")
        if dataset_id and table_id:
            tables_to_check.append({
                "project_id": project_id,
                "dataset_id": dataset_id,
                "table_id": table_id,
            })

    if tool_name in ("get_table_info", "execute_sql") and not tables_to_check:
        return {"error": "Could not identify tables in the request. Please use fully qualified table names (project.dataset.table)."}

    # Check each table against the authorization catalog
    for ref in tables_to_check:
        dataset_id = ref["dataset_id"]
        table_id = ref["table_id"]

        if dataset_id in table_data and table_id in table_data[dataset_id].get("tables", {}):
            allowed_users = table_data[dataset_id]["tables"][table_id].get("authorized_users")
            if allowed_users and user_email not in allowed_users:
                return {
                    "error": f"Access denied: {user_email or 'unknown user'} is not authorized to query "
                             f"`{dataset_id}.{table_id}`. Contact an admin if you need access."
                }

    return None


# Load instructions from the bigquery-skill
_SKILL_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "skills", "bigquery-skill")
_SKILL_MD = os.path.join(_SKILL_DIR, "SKILL.md")
try:
    with open(_SKILL_MD) as f:
        _raw = f.read()
    # Strip YAML frontmatter
    if _raw.startswith("---"):
        parts = _raw.split("---", 2)
        _bq_instruction = parts[2].strip() if len(parts) >= 3 else _raw
    else:
        _bq_instruction = _raw
except FileNotFoundError:
    _bq_instruction = "You are a BigQuery data science agent. Query data to answer business questions."

# Build the agent — only if BigQuery credentials are configured
_bq_toolset = create_bigquery_toolset()

if _bq_toolset:
    bigquery_agent = Agent(
        name="BigQueryAgent",
        model=get_model("BigQueryAgent"),
        description=(
            "Data science agent for business analytics. Queries BigQuery for sales, "
            "inventory, advertising, and operational data. Use for any questions about "
            "ASINs, SKUs, company performance, or business metrics."
        ),
        instruction=_bq_instruction,
        tools=[
            _bq_toolset,
            FunctionTool(func=get_table_data),
            ScratchpadToolset(),
        ],
        before_model_callback=prompt_injection_guardrail,
        before_tool_callback=before_bq_callback,
    )
else:
    bigquery_agent = None
    logger.info("BigQueryAgent disabled: BQ_GCP_SERVICE_ACCOUNT_INFO not configured")
