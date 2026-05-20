"""Tests for the fabrication-detector trigger primitives in
``app.tasks``: ``_bigquery_predicate``, ``_match_triggers``, and
``PROMPT_TRIGGERS``.

Triggered by the 2026-05-20 cron_97f22322 incident; the test suite
pins:

* The actual failing prompt body (FBA Shipment Discrepancy Report)
  triggers the BigQuery required_any group even though it does NOT
  contain the literal word "bigquery". The conjunctive predicate is
  the load-bearing regression — v4's narrow ``\\bbigquery\\b`` keyword
  would have missed the prompt entirely.
* Bare "query" / "SQL" in non-BigQuery contexts (customer query,
  SP-API report query, Neo4j Cypher) does NOT false-flag.
* The dotted-table regex matches the actual table refs used in prod
  (v6's broken regex required ``[a-z0-9_]+`` after the project,
  which never matched ``reports.business_report_asin``).
* Drive UPLOAD phrasing is always "unrunnable" — no registered tool
  satisfies it; read-only drive tools do NOT count.
* Sheets / CSV / SP-API groups each return a sensible required set
  with real registered tool names.
"""

import pytest

from app.tasks import (
    PROMPT_TRIGGERS,
    _BQ_TOOLS,
    _bigquery_predicate,
    _match_triggers,
)


# ---------------------------------------------------------------------------
# _bigquery_predicate
# ---------------------------------------------------------------------------


def test_bigquery_literal_alone_triggers():
    assert _bigquery_predicate("Query BigQuery for sales") == "bigquery"


def test_bigquery_literal_case_insensitive():
    assert _bigquery_predicate("use BIGQUERY for the report") == "bigquery"


def test_dotted_reports_table_alone_triggers():
    prompt = "Identify Top 50 ASINs from `reports.business_report_asin`."
    assert _bigquery_predicate(prompt) == "`reports.business_report_asin`"


def test_dotted_sellercloud_table_alone_triggers():
    prompt = "Query `sellercloud.fba_shipments_partitioned` with the SQL above."
    assert _bigquery_predicate(prompt) == "`sellercloud.fba_shipments_partitioned`"


def test_three_segment_mellanni_medic_table_triggers():
    prompt = "Pull from `mellanni-medic.reports.business_report_asin`."
    assert _bigquery_predicate(prompt) == (
        "`mellanni-medic.reports.business_report_asin`"
    )


def test_query_alone_does_not_trigger():
    """Bare "query" with no BQ companion must NOT trigger — too broad."""
    assert _bigquery_predicate("Answer the customer's query politely") is None


def test_sql_alone_does_not_trigger():
    assert _bigquery_predicate("Review SQL injection guidelines") is None


def test_query_with_bq_companion_token_triggers():
    """The cron_97f22322 prompt body is the canonical case: contains
    "Query" + dotted table — but the dotted-table regex catches it
    first, so synth via a substring that omits the backticks."""
    prompt = (
        "Run the SQL query against the reports. dataset for the top "
        "50 ASINs."
    )
    out = _bigquery_predicate(prompt)
    # Match should mention "query/sql + reports." (companion token).
    assert out is not None and out.startswith("query/sql +")
    assert "reports." in out


def test_query_with_business_report_asin_token_triggers():
    prompt = "Pull the SQL query joining business_report_asin and inventory."
    out = _bigquery_predicate(prompt)
    assert out is not None and "business_report_asin" in out


def test_query_with_unrelated_token_does_not_trigger():
    """Reviewer's v4 #4 example: "customer query" alone must stay quiet."""
    prompt = "Resolve the customer query about refund delays."
    assert _bigquery_predicate(prompt) is None


def test_neo4j_cypher_query_does_not_trigger_bigquery():
    """Cypher prompts mention "query" but should not trip the BigQuery
    required_any group."""
    prompt = "Run a Cypher query against the Neo4j graph to find peers."
    assert _bigquery_predicate(prompt) is None


def test_sp_api_query_does_not_trigger_bigquery():
    prompt = "Make an SP-API query for catalog item B0CABC123."
    assert _bigquery_predicate(prompt) is None


def test_empty_prompt_does_not_trigger():
    assert _bigquery_predicate("") is None
    assert _bigquery_predicate(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _match_triggers — full prompt body
# ---------------------------------------------------------------------------


# Verbatim trimmed body of the cron_97f22322 prompt (kept short — the
# load-bearing tokens are the ones tested explicitly).
CRON_97F22322_PROMPT = """\
Execute the FBA Shipment Discrepancy Report for Top 50 ASINs.

1. Identify Top 50 ASINs by US Revenue (last 30 days) from
   `reports.business_report_asin`.
2. Query `sellercloud.fba_shipments_partitioned` with the following
   SQL logic: ...
5. Create a CSV report including: ShipmentID, ASIN, ...
6. Use AmazonHeadAgent to perform concise analysis and upload the
   CSV to Google Drive.
7. Post final report to Slack.
"""


def test_real_cron_prompt_triggers_bigquery_csv_and_drive_upload():
    """Regression for the 2026-05-20 incident: this prompt body has no
    literal "bigquery" yet must trip the BigQuery required_any group
    via the dotted-table predicate. It also asks for a CSV report and
    a Drive upload — the CSV group must trigger, and Drive upload
    must trip the unrunnable trigger."""
    matched = _match_triggers(CRON_97F22322_PROMPT)
    kinds_by_required: dict[frozenset[str] | None, list[str]] = {}
    for matched_text, required, kind in matched:
        kinds_by_required.setdefault(required, []).append(kind)

    # BigQuery required_any (via dotted table).
    assert _BQ_TOOLS in kinds_by_required
    assert "required_any" in kinds_by_required[_BQ_TOOLS]

    # CSV required_any.
    csv_required = frozenset({"data_to_csv", "export_report_to_csv", "generate_file"})
    assert csv_required in kinds_by_required
    assert "required_any" in kinds_by_required[csv_required]

    # Drive upload UN-RUNNABLE (None as required_any).
    assert None in kinds_by_required
    assert "unrunnable" in kinds_by_required[None]


def test_drive_upload_is_unrunnable_regardless_of_context():
    matched = _match_triggers("Upload the CSV to Google Drive when finished.")
    unrunnable = [t for t in matched if t[2] == "unrunnable"]
    assert unrunnable, "Drive upload phrasing must trip the unrunnable trigger"


def test_drive_upload_short_form_is_unrunnable():
    """Without the literal 'Google' word but still 'upload ... drive'."""
    matched = _match_triggers("Now upload the file to Drive.")
    unrunnable = [t for t in matched if t[2] == "unrunnable"]
    assert unrunnable


def test_drive_read_only_phrasing_does_not_unrunnable():
    """Reading from Drive is fine — only the upload phrasing is the
    structural trap."""
    matched = _match_triggers("Read the file from Google Drive.")
    unrunnable = [t for t in matched if t[2] == "unrunnable"]
    assert not unrunnable


def test_sheets_write_triggers_sheets_group():
    matched = _match_triggers(
        "Append a row to the Google Sheet at https://example.com/sheet"
    )
    required = [t[1] for t in matched if t[1] is not None and "sheets_write" in t[1]]
    assert required


def test_spreadsheet_keyword_triggers_sheets_group():
    matched = _match_triggers("Update the spreadsheet with the new totals.")
    required = [t[1] for t in matched if t[1] is not None and "sheets_write" in t[1]]
    assert required


def test_sp_api_keyword_triggers_sp_api_group():
    matched = _match_triggers("Use the SP-API to pull current inventory.")
    sp_groups = [t[1] for t in matched if t[1] is not None and "sp_request_report" in t[1]]
    assert sp_groups


def test_sp_api_dash_variants():
    """sp-api / sp_api / spapi all match."""
    for variant in ("Use the sp_api to fetch", "Use the spapi to fetch", "Use the sp-api to fetch"):
        matched = _match_triggers(variant)
        sp_groups = [t[1] for t in matched if t[1] is not None and "sp_request_report" in t[1]]
        assert sp_groups, f"variant {variant!r} did not trigger SP-API group"


def test_csv_phrase_triggers_csv_group():
    matched = _match_triggers("Create a CSV report from the rows above.")
    csv_groups = [t[1] for t in matched if t[1] is not None and "data_to_csv" in t[1]]
    assert csv_groups


def test_unrelated_prompt_returns_empty_matches():
    """A prompt with no scheduling-target keywords must not trip any
    detector — preserves the "warn on Google/BigQuery/CSV intent" scope."""
    matched = _match_triggers("Remind Sergey about the call with Aleksandr at 4pm.")
    assert matched == []


def test_empty_prompt_returns_empty():
    assert _match_triggers("") == []
    assert _match_triggers(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# PROMPT_TRIGGERS table sanity
# ---------------------------------------------------------------------------


def test_prompt_triggers_table_shape():
    """Every row is (compiled regex, frozenset|None, kind string)."""
    import re as _re
    for row in PROMPT_TRIGGERS:
        assert len(row) == 3
        pred, required, kind = row
        assert isinstance(pred, _re.Pattern)
        assert required is None or isinstance(required, frozenset)
        assert kind in {"required_any", "unrunnable"}
        if kind == "unrunnable":
            assert required is None, "unrunnable triggers must have required_any=None"
        else:
            assert required and len(required) > 0


def test_required_tool_names_are_lowercase_snake():
    """All tool names in required_any sets are lowercase snake_case,
    matching the runtime registry."""
    import re as _re
    name_re = _re.compile(r"^[a-z][a-z0-9_]+$")
    for _pred, required, kind in PROMPT_TRIGGERS:
        if required is None:
            continue
        for name in required:
            assert name_re.match(name), (
                f"tool name {name!r} does not match registry shape"
            )
    for name in _BQ_TOOLS:
        assert name_re.match(name)
