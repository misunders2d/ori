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


def get_table_data(dataset: str = "", tool_context: ToolContext = None) -> dict:
    """Returns the catalog of available BigQuery datasets and tables.

    Two-level usage to keep the response small:
    - **`get_table_data()`** (no argument): lists ONLY dataset names and
      their high-level descriptions. Use this first to find the dataset
      that matches the user's question.
    - **`get_table_data(dataset="<name>")`**: returns the full table list
      with per-table descriptions for that dataset. Use this AFTER
      picking the right dataset, to choose which table to query.

    Both forms set the discovery flag, so either path satisfies the
    `before_bq_callback` discovery gate.

    Args:
        dataset: Optional dataset name. Empty = dataset summary;
            populated = tables in that dataset.

    Returns:
        dict: `{status, data}` — shape depends on `dataset` argument.
    """
    # Mark the catalog as loaded for this session — `before_bq_callback`
    # in `app/agents/bigquery_agent.py` rejects any data-tool call that
    # comes before this flag is set.
    if tool_context is not None and tool_context.state is not None:
        tool_context.state["bq_catalog_loaded"] = True

    name = (dataset or "").strip()
    if not name:
        # Dataset summary only — small payload (~5KB vs 40KB for the full
        # catalog). Avoids blowing the model's context on a discovery call.
        summary = {
            ds: {"dataset_description": meta.get("dataset_description", "")}
            for ds, meta in table_data.items()
        }
        return {
            "status": "success",
            "data": summary,
            "next_step": (
                "Call `get_table_data(dataset='<name>')` for the dataset "
                "you need, to see its tables and per-table descriptions."
            ),
        }

    if name not in table_data:
        return {
            "status": "error",
            "message": (
                f"Unknown dataset '{name}'. Call `get_table_data()` "
                f"with no argument to see all dataset names."
            ),
        }

    # Full table listing for the requested dataset.
    return {"status": "success", "data": {name: table_data[name]}}
