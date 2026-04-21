"""Tests for app/tools/memory_tools.py — the Neo4j-backed memory + people tools.

Uses the same AsyncMock + patch-driver pattern as tests/test_graph.py. Real
round-trip verification (full-text / embedding) happens during the staging
dry-run against a live Neo4j; these tests cover ACL decisions, Cypher
structure, auto-provisioning order, and error paths.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.tools import memory_tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(user_id: str):
    """Build a MagicMock tool_context with state.to_dict() returning user_id."""
    ctx = MagicMock()
    ctx.state.to_dict.return_value = {"user_id": user_id}
    return ctx


def _mock_session(rows=None, single_row=None):
    """Construct a mock Neo4j AsyncSession.

    `rows` — list of dicts returned by async-iter (for list-returning queries).
    `single_row` — dict returned by result.single() (for single-row queries).
    """
    mock_result = AsyncMock()
    if single_row is not None:
        mock_result.single = AsyncMock(return_value=single_row)
    else:
        mock_result.single = AsyncMock(return_value=None)

    async def mock_aiter(self):
        for r in rows or []:
            yield r

    mock_result.__aiter__ = mock_aiter

    session = AsyncMock()
    session.run = AsyncMock(return_value=mock_result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _mock_driver(session):
    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    return driver


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Reset env + schema flag between tests for deterministic state."""
    from app.core import graph_schema
    graph_schema._schema_ensured = False
    graph_schema._schema_lock = None

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("NEO4J_URI", "neo4j+s://test.neo4j.io")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("COMPANY_DOMAIN", "example.com")
    monkeypatch.setenv("ADMIN_USER_IDS", "admin@example.com,tg_admin_1")
    monkeypatch.setenv("BOT_NAME", "test_bot")
    yield


@pytest.fixture
def patched_driver():
    """Patch _ready_driver to return a mock driver with a mutable session.

    Yields the mock session so tests can inspect session.run.call_args.
    """
    session = _mock_session()
    driver = _mock_driver(session)

    async def _ready(*args, **kwargs):
        return driver

    with patch("app.tools.memory_tools._ready_driver", new=_ready):
        yield session


# ---------------------------------------------------------------------------
# ACL helpers — pure-Python, no DB calls
# ---------------------------------------------------------------------------


class TestAclFlags:
    def test_admin_resolution(self):
        is_admin, is_company = memory_tools._get_acl_flags("admin@example.com")
        assert is_admin is True
        assert is_company is True  # admin email also matches company domain

    def test_telegram_admin_is_admin_not_company(self):
        is_admin, is_company = memory_tools._get_acl_flags("tg_admin_1")
        assert is_admin is True
        assert is_company is False  # no domain match

    def test_company_non_admin(self):
        is_admin, is_company = memory_tools._get_acl_flags("bob@example.com")
        assert is_admin is False
        assert is_company is True

    def test_outsider(self):
        is_admin, is_company = memory_tools._get_acl_flags("stranger@elsewhere.com")
        assert is_admin is False
        assert is_company is False

    def test_telegram_non_admin_outsider(self):
        is_admin, is_company = memory_tools._get_acl_flags("tg_999")
        assert is_admin is False
        assert is_company is False

    def test_company_domain_unset_disables_gate(self, monkeypatch):
        monkeypatch.delenv("COMPANY_DOMAIN", raising=False)
        _, is_company = memory_tools._get_acl_flags("bob@example.com")
        assert is_company is False  # Empty domain → everyone is non-company

    def test_case_insensitive_domain(self):
        _, is_company = memory_tools._get_acl_flags("BOB@EXAMPLE.COM")
        assert is_company is True


# ---------------------------------------------------------------------------
# ACL matrix — read-gate per namespace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "namespace,caller,expected_status",
    [
        # Personal: admins only.
        ("personal", "admin@example.com", "success"),
        ("personal", "tg_admin_1", "success"),
        ("personal", "bob@example.com", "forbidden"),
        ("personal", "stranger@elsewhere.com", "forbidden"),
        ("personal", "tg_999", "forbidden"),
        # Professional: admins or company users.
        ("professional", "admin@example.com", "success"),
        ("professional", "tg_admin_1", "success"),
        ("professional", "bob@example.com", "success"),
        ("professional", "stranger@elsewhere.com", "forbidden"),
        ("professional", "tg_999", "forbidden"),
        # Technical: open to all.
        ("technical", "admin@example.com", "success"),
        ("technical", "tg_admin_1", "success"),
        ("technical", "bob@example.com", "success"),
        ("technical", "stranger@elsewhere.com", "success"),
        ("technical", "tg_999", "success"),
    ],
)
@pytest.mark.asyncio
async def test_search_knowledge_read_acl(
    namespace, caller, expected_status, patched_driver
):
    patched_driver.run = AsyncMock(return_value=_mock_session(rows=[]).run.return_value)
    result = await memory_tools.search_knowledge(
        "query", namespace, top_k=3, tool_context=_make_ctx(caller)
    )
    assert result["status"] == expected_status, f"{caller} on {namespace}: {result}"


@pytest.mark.parametrize(
    "namespace,caller,expected_status",
    [
        ("personal", "bob@example.com", "forbidden"),
        ("professional", "stranger@elsewhere.com", "forbidden"),
        ("professional", "bob@example.com", "success"),
        ("technical", "stranger@elsewhere.com", "success"),
    ],
)
@pytest.mark.asyncio
async def test_list_records_read_acl(namespace, caller, expected_status, patched_driver):
    result = await memory_tools.list_records(namespace, tool_context=_make_ctx(caller))
    assert result["status"] == expected_status


@pytest.mark.parametrize(
    "namespace,caller,expected_status",
    [
        ("personal", "bob@example.com", "forbidden"),
        ("personal", "admin@example.com", "success"),
        ("professional", "tg_999", "forbidden"),
        ("professional", "tg_admin_1", "success"),
    ],
)
@pytest.mark.asyncio
async def test_get_records_read_acl(namespace, caller, expected_status, patched_driver):
    result = await memory_tools.get_records(
        ["mem_x"], namespace, tool_context=_make_ctx(caller)
    )
    assert result["status"] == expected_status


# ---------------------------------------------------------------------------
# Namespace + category validation
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.asyncio
    async def test_search_invalid_namespace(self, patched_driver):
        result = await memory_tools.search_knowledge(
            "q", "bogus", tool_context=_make_ctx("admin@example.com")
        )
        assert result["status"] == "error"
        assert "Invalid namespace" in result["message"]

    @pytest.mark.asyncio
    async def test_create_invalid_namespace(self, patched_driver):
        result = await memory_tools.create_record(
            "bogus", "text", "desc", "memory", [], tool_context=_make_ctx("admin@example.com")
        )
        assert result["status"] == "error"
        assert "Invalid namespace" in result["message"]

    @pytest.mark.asyncio
    async def test_create_invalid_category(self, patched_driver):
        result = await memory_tools.create_record(
            "technical", "text", "desc", "bogus_cat", [],
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"
        assert "Invalid category" in result["message"]

    @pytest.mark.asyncio
    async def test_create_requires_text(self, patched_driver):
        result = await memory_tools.create_record(
            "technical", "", "desc", "memory", [],
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"
        assert "text" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_create_person_invalid_scope(self, patched_driver):
        result = await memory_tools.create_person(
            "Alice", "Smith", "colleague", "[]",
            scopes=["bogus"], tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"
        assert "Invalid scope" in result["message"]

    @pytest.mark.asyncio
    async def test_create_person_requires_first_name(self, patched_driver):
        result = await memory_tools.create_person(
            "", "", "", "[]", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Cypher structure assertions — what Cypher strings do the tools actually run?
# ---------------------------------------------------------------------------


def _assert_query_contains(session_run_mock, *substrings, call_index=-1):
    """Inspect session.run's call at `call_index` and assert query contains substrings."""
    assert session_run_mock.await_count > 0, "session.run was never awaited"
    call = session_run_mock.await_args_list[call_index]
    query = call.args[0] if call.args else ""
    for sub in substrings:
        assert sub in query, f"expected '{sub}' in:\n{query}"


class TestCypherConstruction:
    @pytest.mark.asyncio
    async def test_create_record_uses_scope_label(self, patched_driver):
        # Arrange: session.run for the create returns a row so we don't error.
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"record_id": "mem_test"})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_record(
            "professional", "hello world", "greet", "memory", ["greet"],
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"
        # First call is auto-provision MERGE, then the create.
        _assert_query_contains(
            patched_driver.run, ":Memory:ProfessionalMemory", "genai.vector.encode", call_index=-1
        )

    @pytest.mark.asyncio
    async def test_create_record_auto_provisions_before_creating(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"record_id": "mem_x"})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        await memory_tools.create_record(
            "technical", "some engineering note", "gotcha", "technical", [],
            tool_context=_make_ctx("bob@example.com"),
        )
        calls = patched_driver.run.await_args_list
        assert len(calls) >= 2
        # First call auto-provisions the caller's :Person node.
        first_query = calls[0].args[0]
        assert "MERGE (p:Person {primary_user_id: $caller})" in first_query
        assert ":ProfessionalPerson" in first_query  # bob@example.com → company user

    @pytest.mark.asyncio
    async def test_create_record_outsider_auto_provisions_as_personal(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"record_id": "mem_x"})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        await memory_tools.create_record(
            "technical", "technical note", "tip", "technical", [],
            tool_context=_make_ctx("tg_999"),
        )
        first_query = patched_driver.run.await_args_list[0].args[0]
        assert ":PersonalPerson" in first_query  # tg_999 → non-company

    @pytest.mark.asyncio
    async def test_update_record_uses_authored_match(self, patched_driver):
        # Simulate: caller is NOT the author → match returns no row.
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value=None)
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.update_record(
            "mem_1", "professional",
            json.dumps({"short_description": "new title"}),
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"
        assert "not authored by you" in result["message"]
        # Verify query had the :AUTHORED MATCH predicate.
        query = patched_driver.run.await_args_list[0].args[0]
        assert "[:AUTHORED]" in query
        assert ":ProfessionalMemory" in query

    @pytest.mark.asyncio
    async def test_update_any_record_skips_authored_match(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"record_id": "mem_1"})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.update_any_record(
            "mem_1", "professional",
            json.dumps({"short_description": "admin override"}),
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        query = patched_driver.run.await_args_list[0].args[0]
        assert "[:AUTHORED]" not in query
        assert "MATCH (m:ProfessionalMemory" in query

    @pytest.mark.asyncio
    async def test_update_any_record_rejects_non_admin(self, patched_driver):
        result = await memory_tools.update_any_record(
            "mem_1", "professional",
            json.dumps({"short_description": "hack"}),
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"
        assert "admin-only" in result["message"]

    @pytest.mark.asyncio
    async def test_delete_record_as_admin_uses_label_match(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"deleted": 1})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.delete_record(
            "mem_1", "personal", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        # Admin path skips the :AUTHORED predicate.
        query = patched_driver.run.await_args_list[0].args[0]
        assert ":AUTHORED" not in query
        assert ":PersonalMemory" in query
        assert "DETACH DELETE m" in query

    @pytest.mark.asyncio
    async def test_delete_record_as_author_uses_authored_predicate(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"deleted": 1})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.delete_record(
            "mem_1", "professional", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"
        query = patched_driver.run.await_args_list[0].args[0]
        assert "[:AUTHORED]" in query
        assert ":ProfessionalMemory" in query

    @pytest.mark.asyncio
    async def test_delete_non_author_gets_forbidden(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"deleted": 0})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.delete_record(
            "mem_1", "professional", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"
        assert "not authored by you" in result["message"]


# ---------------------------------------------------------------------------
# People tools
# ---------------------------------------------------------------------------


class TestPeopleTools:
    @pytest.mark.asyncio
    async def test_create_person_default_scope_is_professional(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"person_id": "per_new"})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_person(
            "Alice", "Smith", "colleague",
            json.dumps([{"id_type": "email", "id_value": "alice@example.com"}]),
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        assert result["scopes"] == ["professional"]
        # Create query at the end has :ProfessionalPerson but not :PersonalPerson.
        create_query = patched_driver.run.await_args_list[-1].args[0]
        assert ":ProfessionalPerson" in create_query
        assert ":PersonalPerson" not in create_query

    @pytest.mark.asyncio
    async def test_create_person_dual_scope(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"person_id": "per_dual"})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_person(
            "Bob", "Jones", "friend and colleague",
            json.dumps([{"id_type": "email", "id_value": "bob@example.com"}]),
            scopes=["personal", "professional"],
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        assert sorted(result["scopes"]) == ["personal", "professional"]
        create_query = patched_driver.run.await_args_list[-1].args[0]
        assert ":PersonalPerson" in create_query
        assert ":ProfessionalPerson" in create_query

    @pytest.mark.asyncio
    async def test_search_people_personal_scope_admin_only(self, patched_driver):
        result = await memory_tools.search_people(
            "someone", "personal", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"

    @pytest.mark.asyncio
    async def test_search_people_professional_scope_company_allowed(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            async def aiter(self):
                return
                yield  # unreachable, makes this an async generator
            r.__aiter__ = aiter
            r.single = AsyncMock(return_value=None)
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.search_people(
            "colleague", "professional", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_promote_person_admin_only(self, patched_driver):
        result = await memory_tools.promote_person(
            "per_1", "professional", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"

    @pytest.mark.asyncio
    async def test_promote_person_adds_label(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"person_id": "per_1", "labels": ["Person", "ProfessionalPerson", "PersonalPerson"]})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.promote_person(
            "per_1", "personal", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        query = patched_driver.run.await_args_list[-1].args[0]
        assert "SET p:PersonalPerson" in query


# ---------------------------------------------------------------------------
# Auto-person-id determinism
# ---------------------------------------------------------------------------


class TestAutoPersonId:
    def test_stable_across_calls(self):
        a = memory_tools._auto_person_id("bob@example.com")
        b = memory_tools._auto_person_id("bob@example.com")
        assert a == b
        assert a.startswith("per_auto_")

    def test_different_callers_get_different_ids(self):
        a = memory_tools._auto_person_id("bob@example.com")
        b = memory_tools._auto_person_id("alice@example.com")
        assert a != b


# ---------------------------------------------------------------------------
# Env misconfiguration — tools fail gracefully
# ---------------------------------------------------------------------------


class TestMisconfiguration:
    @pytest.mark.asyncio
    async def test_search_missing_openai_key(self, monkeypatch, patched_driver):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        result = await memory_tools.search_knowledge(
            "q", "technical", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "error"
        assert "OPENAI_API_KEY" in result["message"]

    @pytest.mark.asyncio
    async def test_create_record_missing_openai_key(self, monkeypatch, patched_driver):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        # Need session.run to not raise in auto-provision step
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"record_id": "x"})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)
        result = await memory_tools.create_record(
            "technical", "text", "desc", "memory", [],
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "error"
        assert "OPENAI_API_KEY" in result["message"]

    @pytest.mark.asyncio
    async def test_driver_not_configured(self, monkeypatch):
        monkeypatch.delenv("NEO4J_URI", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        # Don't patch _ready_driver — let the real implementation return None.
        from app.core import graph
        graph._driver = None

        result = await memory_tools.search_knowledge(
            "q", "technical", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "error"
        assert "Neo4j not configured" in result["message"]
