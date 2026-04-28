"""BigQuery sub-agent with per-table access control.

Access control uses email-based authorization lists from `table_data` metadata.
The user's identity (email) is injected into session state by the transport
poller (state key: 'user_id'). Admin users (from ADMIN_USER_IDS) bypass all
table restrictions; the company-domain gate (COMPANY_DOMAIN env) filters
non-admins to internal users only.

ADK 2.0: `before_tool_callback` is still supported on `LlmAgent` for
agent-scoped tool gating. App-level concerns (prompt injection, admin
gating) live in plugins.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
from typing import Any

from google.adk.agents import Agent
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

from app.tools.bigquery_data import get_table_data, table_data
from app.tools.bigquery_tools import create_bigquery_toolset
from app.toolsets import ScratchpadToolset
from app.util.models import get_model

logger = logging.getLogger(__name__)


_BQ_TOOL_NAMES = frozenset({
    "execute_sql",
    "list_datasets",
    "list_tables",
    "list_table_ids",
    "get_table_info",
    "get_dataset_info",
    "forecast",
    "analyze_contribution",
    "detect_anomalies",
    "ask_data_insights",
})

_DATA_TOOLS = {
    "execute_sql", "get_table_info", "forecast",
    "analyze_contribution", "detect_anomalies", "ask_data_insights",
}


def _company_domain() -> str:
    return os.environ.get("COMPANY_DOMAIN", "").strip().lower()


def _admin_ids() -> list[str]:
    return [x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]


def _extract_tables_from_sql(query: str, default_project: str = "") -> list[dict]:
    tables = []
    for table_name in re.findall(r"(?:FROM|JOIN)\s+`?([\w.-]+)`?", query, re.IGNORECASE):
        parts = table_name.split(".")
        if len(parts) == 3:
            tables.append({"project_id": parts[0], "dataset_id": parts[1], "table_id": parts[2]})
        elif len(parts) == 2:
            tables.append({"project_id": default_project, "dataset_id": parts[0], "table_id": parts[1]})
    return tables


_OBSOLETE_MARKERS = ("do not use", "obsolete", "deprecated")


def _is_obsolete(description: str) -> bool:
    """Return True if the catalog description signals the table shouldn't be used."""
    desc = (description or "").lower()
    return any(marker in desc for marker in _OBSOLETE_MARKERS)


def _check_table_access(tables: list[dict], user_email: str) -> dict | None:
    for ref in tables:
        dataset_id = ref["dataset_id"]
        table_id = ref["table_id"]
        if dataset_id in table_data and table_id in table_data[dataset_id].get("tables", {}):
            tbl = table_data[dataset_id]["tables"][table_id]

            # Hard guard: refuse to query tables flagged as obsolete in the
            # catalog (description contains "do not use" / "obsolete" /
            # "deprecated"). The agent's instruction tells it to prefer the
            # recommended replacement; this catches the case where the
            # agent reads but ignores the description anyway.
            description = tbl.get("description", "")
            if _is_obsolete(description):
                return {
                    "error": (
                        f"`{dataset_id}.{table_id}` is flagged as obsolete in "
                        f"the catalog: \"{description}\". "
                        f"Use the recommended replacement (call `get_table_data` "
                        f"and read the description for sibling tables in "
                        f"`{dataset_id}`)."
                    )
                }

            allowed_users = tbl.get("authorized_users")
            if allowed_users and user_email not in allowed_users:
                return {
                    "error": (
                        f"Access denied: {user_email or 'unknown user'} is not authorized to query "
                        f"`{dataset_id}.{table_id}`. Contact an admin if you need access."
                    )
                }
    return None


def before_bq_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict | None:
    """Per-table access control for ALL BigQuery tools."""
    tool_name = getattr(tool, "name", "") or ""
    if tool_name not in _BQ_TOOL_NAMES:
        return None

    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    user_email = state.get("user_id", "") or ""
    is_admin = user_email in _admin_ids()

    if not is_admin:
        domain = _company_domain()
        if domain and (not user_email or not user_email.lower().endswith(f"@{domain}")):
            return {
                "error": (
                    f"Access denied: BigQuery is only available to @{domain} users. "
                    f"Your identity ({user_email or 'unknown'}) is not authorized."
                )
            }

    project_id = args.get("project_id", "")
    tables_to_check = []

    for src_key in ("query", "history_data", "target_data", "input_data"):
        sql = args.get(src_key, "")
        if sql:
            tables_to_check.extend(_extract_tables_from_sql(sql, project_id))

    input_data = args.get("input_data", "")
    if input_data and not tables_to_check:
        parts = input_data.strip("`").split(".")
        if len(parts) >= 2:
            tables_to_check.append({
                "project_id": parts[0] if len(parts) == 3 else project_id,
                "dataset_id": parts[-2],
                "table_id": parts[-1],
            })

    for ref in args.get("table_references", []):
        if isinstance(ref, dict):
            tables_to_check.append({
                "project_id": ref.get("projectId", ref.get("project_id", "")),
                "dataset_id": ref.get("datasetId", ref.get("dataset_id", "")),
                "table_id": ref.get("tableId", ref.get("table_id", "")),
            })

    dataset_id = args.get("dataset_id", "")
    table_id = args.get("table_id", "")
    if dataset_id and table_id:
        tables_to_check.append({
            "project_id": project_id,
            "dataset_id": dataset_id,
            "table_id": table_id,
        })

    if tool_name in _DATA_TOOLS and not tables_to_check:
        return {
            "error": (
                "Could not identify tables in the request. Please use fully qualified "
                "table names (project.dataset.table)."
            )
        }

    return _check_table_access(tables_to_check, user_email)


# Lift instruction from the bigquery-skill SKILL.md.
_SKILLS_DIR = pathlib.Path(__file__).parent.parent.parent / "skills"
_SKILL_MD = _SKILLS_DIR / "bigquery-skill" / "SKILL.md"
try:
    _raw = _SKILL_MD.read_text()
    if _raw.startswith("---"):
        parts = _raw.split("---", 2)
        _bq_instruction = parts[2].strip() if len(parts) >= 3 else _raw
    else:
        _bq_instruction = _raw
except FileNotFoundError:
    _bq_instruction = "You are a BigQuery data science agent. Query data to answer business questions."


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
        before_tool_callback=before_bq_callback,
    )
else:
    bigquery_agent = None
    logger.info("BigQueryAgent disabled: BQ_GCP_SERVICE_ACCOUNT_INFO not configured")
