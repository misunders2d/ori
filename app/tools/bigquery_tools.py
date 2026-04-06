"""BigQuery toolset creation with service account credentials from vault."""

import json
import logging
import os

from google.adk.tools.bigquery import BigQueryCredentialsConfig, BigQueryToolset
from google.adk.tools.bigquery.config import BigQueryToolConfig, WriteMode
from google.oauth2 import service_account

logger = logging.getLogger(__name__)


def create_bigquery_toolset() -> BigQueryToolset | None:
    """Create and return a configured BigQueryToolset, or None if credentials are missing."""
    sa_json = os.environ.get("BQ_GCP_SERVICE_ACCOUNT_INFO", "").strip()
    if not sa_json:
        logger.warning("BigQuery: BQ_GCP_SERVICE_ACCOUNT_INFO not configured, toolset disabled")
        return None

    try:
        sa_info = json.loads(sa_json)
    except json.JSONDecodeError:
        logger.error("BigQuery: BQ_GCP_SERVICE_ACCOUNT_INFO is not valid JSON")
        return None

    credentials = service_account.Credentials.from_service_account_info(sa_info)

    return BigQueryToolset(
        credentials_config=BigQueryCredentialsConfig(credentials=credentials),
        bigquery_tool_config=BigQueryToolConfig(
            write_mode=WriteMode.BLOCKED,
            max_query_result_rows=10000,
        ),
    )
