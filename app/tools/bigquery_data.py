"""BigQuery table metadata catalog — loaded from bigquery-skill.

Table metadata lives in skills/bigquery-skill/references/table_data.json.
Each dataset contains tables with optional 'authorized_users' lists
for per-table email-based access control.
"""

import json
import logging
import os

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TABLE_DATA_FILE = os.path.join(_PROJECT_ROOT, "skills", "bigquery-skill", "references", "table_data.json")

# Load table metadata at import time
try:
    with open(_TABLE_DATA_FILE) as f:
        table_data: dict = json.load(f)
except (FileNotFoundError, json.JSONDecodeError) as e:
    logger.warning("BigQuery table_data not loaded: %s", e)
    table_data = {}


def get_table_data(tool_context: ToolContext = None) -> dict:
    """Returns the catalog of available BigQuery datasets and tables.

    Use this to discover what data is available before writing queries.
    Some tables have descriptions explaining their content and usage.

    Returns:
        dict: Dataset and table metadata.
    """
    # Mark the catalog as loaded for this session — `before_bq_callback`
    # in `app/agents/bigquery_agent.py` rejects any data-tool call that
    # comes before this flag is set, so the agent must always inspect
    # descriptions before picking a table.
    if tool_context is not None and tool_context.state is not None:
        tool_context.state["bq_catalog_loaded"] = True
    return {"status": "success", "data": table_data}
