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


def _extract_tables_from_sql(query: str, default_project: str = "") -> list[dict]:
    """Extract all table references from a SQL string."""
    tables = []
    for table_name in re.findall(r"(?:FROM|JOIN)\s+`?([\w.-]+)`?", query, re.IGNORECASE):
        parts = table_name.split(".")
        if len(parts) == 3:
            tables.append({"project_id": parts[0], "dataset_id": parts[1], "table_id": parts[2]})
        elif len(parts) == 2:
            tables.append({"project_id": default_project, "dataset_id": parts[0], "table_id": parts[1]})
    return tables


def _check_table_access(tables: list[dict], user_email: str) -> dict | None:
    """Check if user has access to all listed tables. Returns error dict or None."""
    for ref in tables:
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


def _get_company_domain() -> str:
    """Return the company domain for BQ access gating (e.g. 'mellanni.com')."""
    return os.environ.get("COMPANY_DOMAIN", "").strip().lower()


def _get_admin_ids() -> list[str]:
    return [x.strip() for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()]


# The BigQuery-specific tools registered by Google ADK's BigQueryToolset.
# Anything outside this set (planner, scratchpad, skill lookup, etc.) that
# happens to flow through this agent's before_tool_callback during a
# scheduled or coordinator-delegated run passes through unconditionally.
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


def before_bq_callback(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict | None:
    """Per-table access control for ALL BigQuery tools.

    Gates every tool in the BigQuery toolset. First checks that the user
    belongs to the company domain (COMPANY_DOMAIN env var). Then extracts
    table references from SQL queries, explicit args, and table_references
    lists, and checks the user's email against authorized_users in the
    table catalog.

    Scoped to actual BigQuery tools (``_BQ_TOOL_NAMES``). Cross-cutting tools
    invoked in this agent's context (planner, scratchpad, etc.) pass through
    so scheduled tasks don't fail on unrelated tool calls.

    Admins bypass the company-domain gate. Per-table allowlists still apply
    (admins should get the tables they're listed on; that logic runs below).
    """
    tool_name = getattr(tool, "name", "") or ""
    if tool_name not in _BQ_TOOL_NAMES:
        return None  # Not a BigQuery tool → not gated by this callback.

    state = tool_context.state.to_dict() if hasattr(tool_context.state, "to_dict") else {}
    user_email = state.get("user_id", "") or ""
    is_admin = user_email in _get_admin_ids()

    # Domain-level gate: user must belong to the company (admins bypass)
    if not is_admin:
        company_domain = _get_company_domain()
        if company_domain:
            if not user_email or not user_email.lower().endswith(f"@{company_domain}"):
                return {
                    "error": f"Access denied: BigQuery is only available to @{company_domain} users. "
                             f"Your identity ({user_email or 'unknown'}) is not authorized."
                }
    project_id = args.get("project_id", "")
    tables_to_check = []

    # 1. Extract from SQL in 'query' param (execute_sql)
    query = args.get("query", "")
    if query:
        tables_to_check.extend(_extract_tables_from_sql(query, project_id))

    # 2. Extract from 'history_data' param (forecast, detect_anomalies — accepts SQL)
    history_data = args.get("history_data", "")
    if history_data:
        tables_to_check.extend(_extract_tables_from_sql(history_data, project_id))

    # 3. Extract from 'target_data' param (detect_anomalies — optional SQL)
    target_data = args.get("target_data", "")
    if target_data:
        tables_to_check.extend(_extract_tables_from_sql(target_data, project_id))

    # 4. Extract from 'input_data' param (analyze_contribution — table ref or SQL)
    input_data = args.get("input_data", "")
    if input_data:
        tables_to_check.extend(_extract_tables_from_sql(input_data, project_id))
        # input_data can also be a bare table reference like "dataset.table"
        if not tables_to_check:
            parts = input_data.strip("`").split(".")
            if len(parts) >= 2:
                tables_to_check.append({
                    "project_id": parts[0] if len(parts) == 3 else project_id,
                    "dataset_id": parts[-2],
                    "table_id": parts[-1],
                })

    # 5. Extract from 'table_references' param (ask_data_insights)
    for ref in args.get("table_references", []):
        if isinstance(ref, dict):
            tables_to_check.append({
                "project_id": ref.get("projectId", ref.get("project_id", "")),
                "dataset_id": ref.get("datasetId", ref.get("dataset_id", "")),
                "table_id": ref.get("tableId", ref.get("table_id", "")),
            })

    # 6. Explicit dataset_id + table_id args (get_table_info, list_table_ids)
    dataset_id = args.get("dataset_id", "")
    table_id = args.get("table_id", "")
    if dataset_id and table_id:
        tables_to_check.append({
            "project_id": project_id,
            "dataset_id": dataset_id,
            "table_id": table_id,
        })

    # For tools that must reference tables, block if we couldn't identify any
    _DATA_TOOLS = {"execute_sql", "get_table_info", "forecast", "analyze_contribution", "detect_anomalies", "ask_data_insights"}
    if tool_name in _DATA_TOOLS and not tables_to_check:
        return {"error": "Could not identify tables in the request. Please use fully qualified table names (project.dataset.table)."}

    # For list/discovery tools on restricted datasets, filter what's visible
    if tool_name == "list_table_ids" and dataset_id and dataset_id in table_data:
        restricted_tables = [
            tid for tid, tinfo in table_data[dataset_id].get("tables", {}).items()
            if tinfo.get("authorized_users") and user_email not in tinfo["authorized_users"]
        ]
        if restricted_tables:
            # Let the tool run, but log the restriction — the agent instruction
            # should guide it to only show authorized tables via get_table_data
            pass

    return _check_table_access(tables_to_check, user_email)


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
