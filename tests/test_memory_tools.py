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


def _is_resolve_caller_lookup(query: str) -> bool:
    """True if a query is the alias-aware resolve-caller lookup."""
    return "$caller IN coalesce(p.aliases" in query


def _dispatched_run(record_row=None, resolve_hit=None):
    """A session.run mock that disambiguates between:
    - the resolve-caller lookup → returns `resolve_hit` (None = miss, MERGE then runs)
    - every other query → returns `record_row`

    Use this for tests that only care about the main query outcome.
    Scripted test sequences should continue to use a local `_run` with an iterator.
    """
    async def _run(*args, **kwargs):
        query = args[0] if args else ""
        r = AsyncMock()
        if _is_resolve_caller_lookup(query):
            r.single = AsyncMock(return_value=resolve_hit)
        else:
            r.single = AsyncMock(return_value=record_row)
        return r
    return AsyncMock(side_effect=_run)


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
    async def test_create_record_alias_hit_skips_merge(self, patched_driver):
        """If the caller's id matches an existing :Person by primary or alias,
        MERGE is skipped — the create uses the canonical primary_user_id."""
        patched_driver.run = _dispatched_run(
            record_row={"record_id": "mem_x"},
            resolve_hit={"canonical": "sergey@mellanni.com"},
        )
        await memory_tools.create_record(
            "technical", "some engineering note", "gotcha", "technical", [],
            tool_context=_make_ctx("tg_330959414"),  # alias for sergey@mellanni.com
        )
        calls = patched_driver.run.await_args_list
        lookup_call = next(
            (c for c in calls if _is_resolve_caller_lookup(c.args[0])), None
        )
        merge_call = next(
            (c for c in calls if "MERGE (p:Person {primary_user_id: $caller})" in c.args[0]),
            None,
        )
        assert lookup_call is not None
        assert merge_call is None, "MERGE should be skipped when alias lookup hits"

    @pytest.mark.asyncio
    async def test_create_record_alias_miss_runs_merge(self, patched_driver):
        """New caller (no existing :Person) → MERGE runs with correct scope label."""
        patched_driver.run = _dispatched_run(
            record_row={"record_id": "mem_x"},
            resolve_hit=None,  # miss
        )
        await memory_tools.create_record(
            "technical", "engineering note", "gotcha", "technical", [],
            tool_context=_make_ctx("bob@example.com"),
        )
        calls = patched_driver.run.await_args_list
        merge_call = next(
            (c for c in calls if "MERGE (p:Person {primary_user_id: $caller})" in c.args[0]),
            None,
        )
        assert merge_call is not None
        assert ":ProfessionalPerson" in merge_call.args[0]  # bob@ → company

    @pytest.mark.asyncio
    async def test_create_record_new_caller_runs_merge_with_scope(self, patched_driver):
        # Simulate: first call (resolve-caller lookup) returns None → new caller,
        # MERGE runs; subsequent calls return a record_id for the create.
        call_seq = iter([None, None, {"record_id": "mem_new"}])

        async def _run(*args, **kwargs):
            r = AsyncMock()
            try:
                r.single = AsyncMock(return_value=next(call_seq))
            except StopIteration:
                r.single = AsyncMock(return_value={"record_id": "mem_extra"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        await memory_tools.create_record(
            "technical", "some engineering note", "gotcha", "technical", [],
            tool_context=_make_ctx("bob@example.com"),
        )
        calls = patched_driver.run.await_args_list
        merge_call = next(
            (c for c in calls if "MERGE (p:Person {primary_user_id: $caller})" in c.args[0]),
            None,
        )
        assert merge_call is not None
        # Company user → professional scope label.
        assert ":ProfessionalPerson" in merge_call.args[0]

    @pytest.mark.asyncio
    async def test_create_record_outsider_auto_provisions_as_personal(self, patched_driver):
        call_seq = iter([None, None, {"record_id": "mem_x"}])

        async def _run(*args, **kwargs):
            r = AsyncMock()
            try:
                r.single = AsyncMock(return_value=next(call_seq))
            except StopIteration:
                r.single = AsyncMock(return_value={"record_id": "mem_extra"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        await memory_tools.create_record(
            "technical", "technical note", "tip", "technical", [],
            tool_context=_make_ctx("tg_999"),
        )
        calls = patched_driver.run.await_args_list
        merge_call = next(
            (c for c in calls if "MERGE (p:Person {primary_user_id: $caller})" in c.args[0]),
            None,
        )
        assert merge_call is not None
        assert ":PersonalPerson" in merge_call.args[0]  # tg_999 → non-company

    @pytest.mark.asyncio
    async def test_update_record_uses_authored_match(self, patched_driver):
        # Simulate: caller is NOT the author → main-query match returns no row.
        patched_driver.run = _dispatched_run(record_row=None, resolve_hit=None)

        result = await memory_tools.update_record(
            "mem_1", "professional",
            json.dumps({"short_description": "new title"}),
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"
        assert "not authored by you" in result["message"]
        # Find the author-gated UPDATE query among all calls.
        authored_query = next(
            (c.args[0] for c in patched_driver.run.await_args_list
             if "(m:ProfessionalMemory" in c.args[0]
             and "m.author_user_id = $caller_id" in c.args[0]),
            None,
        )
        assert authored_query is not None, (
            "expected an author_user_id predicate on :ProfessionalMemory"
        )

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
        assert "author_user_id" not in query
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
        # Admin path skips the author_user_id predicate.
        query = patched_driver.run.await_args_list[0].args[0]
        assert "author_user_id" not in query
        assert ":PersonalMemory" in query
        assert "DETACH DELETE m" in query

    @pytest.mark.asyncio
    async def test_delete_record_as_author_uses_authored_predicate(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={"deleted": 1}, resolve_hit={"canonical": "bob@example.com"}
        )

        result = await memory_tools.delete_record(
            "mem_1", "professional", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"
        authored_query = next(
            (c.args[0] for c in patched_driver.run.await_args_list
             if "(m:ProfessionalMemory" in c.args[0]
             and "m.author_user_id = $caller_id" in c.args[0]
             and "DETACH DELETE m" in c.args[0]),
            None,
        )
        assert authored_query is not None

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


# ---------------------------------------------------------------------------
# Alias-aware resolution
# ---------------------------------------------------------------------------


class TestResolveCallerPerson:
    @pytest.mark.asyncio
    async def test_lookup_hit_returns_canonical(self, patched_driver):
        """When an existing :Person's aliases or primary matches, return canonical."""
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"canonical": "sergey@mellanni.com"})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        from app.core import graph
        graph._driver = MagicMock()  # truthy placeholder
        canonical = await memory_tools._resolve_caller_person(
            MagicMock(session=MagicMock(return_value=_mock_session(single_row={"canonical": "sergey@mellanni.com"}))),
            "tg_330959414",
            is_company=False,
        )
        assert canonical == "sergey@mellanni.com"

    @pytest.mark.asyncio
    async def test_lookup_miss_creates_new_and_returns_caller(self, patched_driver):
        """When no :Person matches, MERGE new and return raw caller as primary."""
        call_seq = iter([None])  # lookup returns no row

        async def _run(*args, **kwargs):
            r = AsyncMock()
            try:
                r.single = AsyncMock(return_value=next(call_seq))
            except StopIteration:
                r.single = AsyncMock(return_value=None)
            return r

        fake_driver = MagicMock()
        fake_session = _mock_session()

        async def _session_run(*args, **kwargs):
            try:
                row = next(call_seq)
            except StopIteration:
                row = None
            result = AsyncMock()
            result.single = AsyncMock(return_value=row)
            return result

        fake_session.run = AsyncMock(side_effect=_session_run)
        fake_driver.session = MagicMock(return_value=fake_session)

        canonical = await memory_tools._resolve_caller_person(
            fake_driver, "new_user@example.com", is_company=True,
        )
        assert canonical == "new_user@example.com"
        # Verify two calls: lookup + MERGE
        assert fake_session.run.await_count == 2
        merge_query = fake_session.run.await_args_list[1].args[0]
        assert "MERGE (p:Person {primary_user_id: $caller})" in merge_query
        assert ":ProfessionalPerson" in merge_query

    @pytest.mark.asyncio
    async def test_unknown_caller_returns_unchanged(self, patched_driver):
        canonical = await memory_tools._resolve_caller_person(MagicMock(), "unknown", True)
        assert canonical == "unknown"
        canonical = await memory_tools._resolve_caller_person(MagicMock(), "", True)
        assert canonical == ""


# ---------------------------------------------------------------------------
# delete_person + delete_any_person
# ---------------------------------------------------------------------------


class TestDeletePerson:
    @pytest.mark.asyncio
    async def test_delete_as_author(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"deleted": 1})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.delete_person(
            "per_alice", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"
        assert "per_alice" in result["message"]
        # Author path gates on author_user_id property.
        delete_query = next(
            c.args[0] for c in patched_driver.run.await_args_list
            if "DETACH DELETE p" in c.args[0]
        )
        assert "p.author_user_id = $caller_id" in delete_query

    @pytest.mark.asyncio
    async def test_delete_non_author_forbidden(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"deleted": 0})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.delete_person(
            "per_alice", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"
        assert "not authored by you" in result["message"]

    @pytest.mark.asyncio
    async def test_delete_as_admin_uses_plain_match(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"deleted": 1})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.delete_person(
            "per_alice", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        delete_query = next(
            c.args[0] for c in patched_driver.run.await_args_list
            if "DETACH DELETE p" in c.args[0]
        )
        assert "author_user_id" not in delete_query

    @pytest.mark.asyncio
    async def test_delete_any_person_admin_only(self, patched_driver):
        result = await memory_tools.delete_any_person(
            "per_alice", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"

    @pytest.mark.asyncio
    async def test_delete_any_person_not_found(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"deleted": 0})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.delete_any_person(
            "per_ghost", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"
        assert "not found" in result["message"]


# ---------------------------------------------------------------------------
# merge_persons
# ---------------------------------------------------------------------------


class TestMergePersons:
    @pytest.mark.asyncio
    async def test_admin_only(self, patched_driver):
        result = await memory_tools.merge_persons(
            "per_canonical", "per_alias", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"

    @pytest.mark.asyncio
    async def test_same_id_rejected(self, patched_driver):
        result = await memory_tools.merge_persons(
            "per_same", "per_same", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"
        assert "different" in result["message"]

    @pytest.mark.asyncio
    async def test_happy_path(self, patched_driver):
        # Phase 1 reassign-authored returns moved count.
        # Phase 1b reassign-involves returns moved count.
        # Phase 2 finalize returns canonical details.
        call_seq = iter([
            {"moved": 3},  # authored
            {"moved": 1},  # involves
            {
                "person_id": "per_canon",
                "primary_user_id": "tg_330959414",
                "aliases": ["Telegram: 330959414"],
            },
        ])

        async def _run(*args, **kwargs):
            r = AsyncMock()
            try:
                r.single = AsyncMock(return_value=next(call_seq))
            except StopIteration:
                r.single = AsyncMock(return_value=None)
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.merge_persons(
            "per_canon", "per_alias", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        assert result["authored_records_rewritten"] == 3
        assert result["involves_edges_moved"] == 1
        assert "Telegram: 330959414" in result["aliases"]
        # All three phases ran.
        queries = [c.args[0] for c in patched_driver.run.await_args_list]
        assert any(
            "SET t.author_user_id = canon.primary_user_id" in q for q in queries
        )
        assert any("MERGE (x)-[:INVOLVES]->(canon)" in q for q in queries)
        assert any("DETACH DELETE alias" in q for q in queries)

    @pytest.mark.asyncio
    async def test_canonical_not_found(self, patched_driver):
        # Phase 1 finds no canonical/alias pair → single() returns None.
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value=None)
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.merge_persons(
            "per_missing", "per_alias", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"
        assert "not found" in result["message"]


# ---------------------------------------------------------------------------
# Identity gate: create_record / create_person refuse unidentified callers
# ---------------------------------------------------------------------------


class TestIdentityGate:
    """An auto-provisioned :Person stub with no first_name cannot write."""

    @pytest.mark.asyncio
    async def test_has_identity_curated(self):
        # Curated persons (is_auto_provisioned=false) are always identified.
        assert memory_tools._has_identity({"is_auto_provisioned": False}) is True
        assert memory_tools._has_identity(
            {"is_auto_provisioned": False, "first_name": None}
        ) is True

    @pytest.mark.asyncio
    async def test_has_identity_auto_provisioned_with_first_name(self):
        assert memory_tools._has_identity(
            {"is_auto_provisioned": True, "first_name": "Sergey"}
        ) is True

    @pytest.mark.asyncio
    async def test_has_identity_bare_stub(self):
        assert memory_tools._has_identity(
            {"is_auto_provisioned": True, "first_name": None}
        ) is False
        assert memory_tools._has_identity(
            {"is_auto_provisioned": True, "first_name": ""}
        ) is False
        assert memory_tools._has_identity(
            {"is_auto_provisioned": True, "first_name": "   "}
        ) is False

    @pytest.mark.asyncio
    async def test_has_identity_missing_info(self):
        # None and empty dict both mean "no info" → block (safer default).
        assert memory_tools._has_identity(None) is False
        assert memory_tools._has_identity({}) is False

    @pytest.mark.asyncio
    async def test_create_record_rejects_bare_stub(self, patched_driver):
        # Custom dispatcher: resolve-lookup misses, _get_person_info returns a
        # bare auto-provisioned stub → identity gate should block the write.
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value=None)  # miss → provision
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_auto_stub",
                    "first_name": None,
                    "last_name": None,
                    "full_name": "tg_999",
                    "is_auto_provisioned": True,
                })
            else:
                r.single = AsyncMock(return_value={"record_id": "mem_x"})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_record(
            "technical", "text", "desc", "memory", [],
            tool_context=_make_ctx("tg_999"),
        )
        assert result["status"] == "needs_identity"
        assert "per_auto_stub" in result["person_id"]
        assert "update_person" in result["message"]

    @pytest.mark.asyncio
    async def test_create_record_allows_identified_caller(self, patched_driver):
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "sergey@mellanni.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_sergey",
                    "first_name": "Sergey",
                    "last_name": "Demchenko",
                    "full_name": "Sergey Demchenko",
                    "is_auto_provisioned": False,
                })
            else:
                r.single = AsyncMock(return_value={"record_id": "mem_ok"})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_record(
            "technical", "text", "desc", "memory", [],
            tool_context=_make_ctx("sergey@mellanni.com"),
        )
        assert result["status"] == "success"
        assert result["record_id"] == "mem_ok"

    @pytest.mark.asyncio
    async def test_create_person_rejects_bare_stub(self, patched_driver):
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value=None)
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_auto_stub",
                    "first_name": None,
                    "last_name": None,
                    "full_name": "tg_999",
                    "is_auto_provisioned": True,
                })
            else:
                r.single = AsyncMock(return_value={"person_id": "per_new"})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_person(
            "Alice", "Smith", "colleague", "[]",
            tool_context=_make_ctx("tg_999"),
        )
        assert result["status"] == "needs_identity"


# ---------------------------------------------------------------------------
# Pre-create dedup gate on create_person
# ---------------------------------------------------------------------------


class TestDedupGate:
    @pytest.mark.asyncio
    async def test_returns_possible_duplicate(self, patched_driver):
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()

            # resolve-caller lookup → caller is a curated, identified person.
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "admin@example.com"})
            # _get_person_info (caller identity check) → identified.
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_admin",
                    "first_name": "Admin",
                    "last_name": "User",
                    "full_name": "Admin User",
                    "is_auto_provisioned": False,
                })
            # duplicate-check query returns one match.
            elif "WHERE p.is_auto_provisioned = false" in query and "LIMIT 5" in query:
                async def aiter(self):
                    yield {
                        "person_id": "per_igor",
                        "full_name": "Igor Poluyko",
                        "first_name": "Igor",
                        "last_name": "Poluyko",
                        "role": "colleague",
                        "user_ids": '[{"id_type":"email","id_value":"igor@example.com"}]',
                        "labels": ["Person", "ProfessionalPerson"],
                    }
                r.__aiter__ = aiter
                r.single = AsyncMock(return_value=None)
            else:
                r.single = AsyncMock(return_value={"person_id": "per_created"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_person(
            "Igor", "", "some role", "[]",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "possible_duplicate"
        assert len(result["matches"]) == 1
        assert result["matches"][0]["full_name"] == "Igor Poluyko"
        assert "force_create=true" in result["message"]

    @pytest.mark.asyncio
    async def test_force_create_bypasses_dedup(self, patched_driver):
        # Dedup would have matched, but force_create=True skips the check entirely.
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "admin@example.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_admin",
                    "first_name": "Admin",
                    "last_name": "",
                    "full_name": "Admin",
                    "is_auto_provisioned": False,
                })
            elif "LIMIT 5" in query:
                # Shouldn't be called when force_create=True — but be safe.
                async def aiter(self):
                    yield {
                        "person_id": "per_igor",
                        "full_name": "Igor Poluyko",
                        "first_name": "Igor",
                        "last_name": "Poluyko",
                        "role": "",
                        "user_ids": "[]",
                        "labels": ["Person", "ProfessionalPerson"],
                    }
                r.__aiter__ = aiter
                r.single = AsyncMock(return_value=None)
            else:
                r.single = AsyncMock(return_value={"person_id": "per_igor2"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_person(
            "Igor", "Different", "colleague", "[]",
            force_create=True,
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        assert result["person_id"] == "per_igor2"

    @pytest.mark.asyncio
    async def test_no_match_proceeds_without_force(self, patched_driver):
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "admin@example.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_admin",
                    "first_name": "Admin",
                    "last_name": "",
                    "full_name": "Admin",
                    "is_auto_provisioned": False,
                })
            elif "LIMIT 5" in query:
                async def aiter(self):
                    return
                    yield  # unreachable — produces empty async iterator
                r.__aiter__ = aiter
                r.single = AsyncMock(return_value=None)
            else:
                r.single = AsyncMock(return_value={"person_id": "per_fresh"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_person(
            "Uniquename", "Lastname", "colleague", "[]",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Semantic dedup gate on create_record
# ---------------------------------------------------------------------------


def _is_memory_dedup_query(query: str) -> bool:
    """Identify the memory-dedup vector-search query by its unique fragments."""
    return "db.index.vector.queryNodes" in query and "WHERE score >= $threshold" in query


class TestMemoryDedupGate:
    @pytest.mark.asyncio
    async def test_returns_possible_duplicate_on_match(self, patched_driver):
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "sergey@mellanni.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_sergey",
                    "first_name": "Sergey",
                    "last_name": "Demchenko",
                    "full_name": "Sergey Demchenko",
                    "is_auto_provisioned": False,
                })
            elif _is_memory_dedup_query(query):
                async def aiter(self):
                    yield {
                        "record_id": "mem_2026_04_21_existing",
                        "short_description": "Image Naming Convention (Apr 2025)",
                        "text_preview": "Structure: [Product Name] - [Color] - [Size] - [View]...",
                        "created_at": "2026-04-21T15:37:00Z",
                        "score": 0.94,
                    }
                r.__aiter__ = aiter
                r.single = AsyncMock(return_value=None)
            else:
                r.single = AsyncMock(return_value={"record_id": "mem_new"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_record(
            "professional",
            "Convention for image naming: Product then Color then Size then View then Position.",
            "Image naming convention restatement",
            "knowledge",
            ["naming", "images"],
            tool_context=_make_ctx("sergey@mellanni.com"),
        )
        assert result["status"] == "possible_duplicate"
        assert result["threshold"] == 0.92
        assert len(result["matches"]) == 1
        assert result["matches"][0]["record_id"] == "mem_2026_04_21_existing"
        assert "force_create=true" in result["message"]

    @pytest.mark.asyncio
    async def test_force_create_bypasses_dedup(self, patched_driver):
        dedup_was_called = []

        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "sergey@mellanni.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_sergey",
                    "first_name": "Sergey",
                    "last_name": "Demchenko",
                    "full_name": "Sergey Demchenko",
                    "is_auto_provisioned": False,
                })
            elif _is_memory_dedup_query(query):
                dedup_was_called.append(True)
                async def aiter(self):
                    yield {"record_id": "mem_existing", "score": 0.99}
                r.__aiter__ = aiter
                r.single = AsyncMock(return_value=None)
            else:
                r.single = AsyncMock(return_value={"record_id": "mem_forced"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_record(
            "professional", "Some text", "desc", "knowledge", [],
            force_create=True,
            tool_context=_make_ctx("sergey@mellanni.com"),
        )
        assert result["status"] == "success"
        assert result["record_id"] == "mem_forced"
        # Dedup query must never have been executed when force_create=True.
        assert dedup_was_called == []

    @pytest.mark.asyncio
    async def test_no_near_matches_proceeds(self, patched_driver):
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "sergey@mellanni.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_sergey",
                    "first_name": "Sergey",
                    "last_name": "Demchenko",
                    "full_name": "Sergey Demchenko",
                    "is_auto_provisioned": False,
                })
            elif _is_memory_dedup_query(query):
                async def aiter(self):
                    return
                    yield  # empty iterator
                r.__aiter__ = aiter
                r.single = AsyncMock(return_value=None)
            else:
                r.single = AsyncMock(return_value={"record_id": "mem_fresh"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_record(
            "professional", "Brand new topic", "desc", "knowledge", [],
            tool_context=_make_ctx("sergey@mellanni.com"),
        )
        assert result["status"] == "success"
        assert result["record_id"] == "mem_fresh"

    @pytest.mark.asyncio
    async def test_dedup_failure_fails_open(self, patched_driver):
        # If the dedup query itself raises (empty index edge case, plugin
        # hiccup), the helper returns [] and the write proceeds.
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            if _is_memory_dedup_query(query):
                raise RuntimeError("simulated plugin failure")
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "sergey@mellanni.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_sergey",
                    "first_name": "Sergey",
                    "last_name": "Demchenko",
                    "full_name": "Sergey Demchenko",
                    "is_auto_provisioned": False,
                })
            else:
                r.single = AsyncMock(return_value={"record_id": "mem_despite_dedup_fail"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_record(
            "professional", "Some text", "desc", "knowledge", [],
            tool_context=_make_ctx("sergey@mellanni.com"),
        )
        assert result["status"] == "success"
        assert result["record_id"] == "mem_despite_dedup_fail"


# ---------------------------------------------------------------------------
# Pure-Python helpers — canonicalization + sanitization
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_canonicalize_entity_type_basic(self):
        assert memory_tools._canonicalize_entity_type("Brand") == "brand"
        assert memory_tools._canonicalize_entity_type("BRAND") == "brand"
        assert memory_tools._canonicalize_entity_type("Company Name") == "company_name"
        assert memory_tools._canonicalize_entity_type("product-line") == "product_line"

    def test_canonicalize_entity_type_drops_invalid(self):
        assert memory_tools._canonicalize_entity_type("Product/Line!") == "productline"

    def test_canonicalize_entity_type_empty(self):
        assert memory_tools._canonicalize_entity_type("") == ""
        assert memory_tools._canonicalize_entity_type("   ") == ""
        assert memory_tools._canonicalize_entity_type(None) == ""

    def test_sanitize_relation_type_basic(self):
        assert memory_tools._sanitize_relation_type("manages") == "MANAGES"
        assert memory_tools._sanitize_relation_type("reports to") == "REPORTS_TO"
        assert memory_tools._sanitize_relation_type("Part_Of") == "PART_OF"

    def test_sanitize_relation_type_fallback(self):
        assert memory_tools._sanitize_relation_type("") == "RELATED_TO"
        assert memory_tools._sanitize_relation_type("---") == "RELATED_TO"
        assert memory_tools._sanitize_relation_type(None) == "RELATED_TO"


# ---------------------------------------------------------------------------
# relate_persons
# ---------------------------------------------------------------------------


class TestRelatePersons:
    @pytest.mark.asyncio
    async def test_rejects_self_link(self, patched_driver):
        result = await memory_tools.relate_persons(
            "per_a", "per_a", "manages",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"
        assert "must differ" in result["message"]

    @pytest.mark.asyncio
    async def test_author_path_success(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={
                "from_id": "per_a", "to_id": "per_b",
                "relation_type": "MANAGES", "outcome": "created",
            },
            resolve_hit={"canonical": "bob@example.com"},
        )
        result = await memory_tools.relate_persons(
            "per_a", "per_b", "manages",
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"
        assert result["relation_type"] == "MANAGES"
        assert result["outcome"] == "created"
        # Non-admin path: query must include author_user_id predicate on `from`.
        queries = [c.args[0] for c in patched_driver.run.await_args_list]
        assert any(
            "a.author_user_id = $caller" in q and "MERGE (a)-[r:MANAGES]->(b)" in q
            for q in queries
        )

    @pytest.mark.asyncio
    async def test_admin_path_skips_author_gate(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={
                "from_id": "per_a", "to_id": "per_b",
                "relation_type": "MANAGES", "outcome": "existing",
            },
            resolve_hit={"canonical": "admin@example.com"},
        )
        result = await memory_tools.relate_persons(
            "per_a", "per_b", "MANAGES",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        merge_q = next(
            q for q in (c.args[0] for c in patched_driver.run.await_args_list)
            if "MERGE (a)-[r:MANAGES]->(b)" in q
        )
        # Admin path has no author gate in the MATCH — only link_author_user_id
        # on the created edge. Non-admin path would have `a.author_user_id = $caller`.
        assert "a.author_user_id" not in merge_q

    @pytest.mark.asyncio
    async def test_non_author_forbidden(self, patched_driver):
        # MERGE returns no row; existence query confirms `from` exists but
        # was authored by someone else.
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "bob@example.com"})
            elif "MERGE (a)-[r:" in query:
                r.single = AsyncMock(return_value=None)  # gate failed
            elif "a.author_user_id AS author_user_id" in query:
                r.single = AsyncMock(
                    return_value={"author_user_id": "someone_else@example.com"}
                )
            else:
                r.single = AsyncMock(return_value=None)
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.relate_persons(
            "per_a", "per_b", "manages",
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"
        assert "not authored by you" in result["message"]

    @pytest.mark.asyncio
    async def test_from_not_found(self, patched_driver):
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "bob@example.com"})
            else:
                # Main MERGE and existence-check queries both return None.
                r.single = AsyncMock(return_value=None)
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.relate_persons(
            "per_ghost", "per_b", "manages",
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "error"
        assert "not found" in result["message"]


# ---------------------------------------------------------------------------
# create_entity / search_entities / update_entity / delete_entity
# ---------------------------------------------------------------------------


def _is_entity_dedup_query(query: str) -> bool:
    return "entity_embedding" in query and "score >= $threshold" in query


class TestCreateEntity:
    @pytest.mark.asyncio
    async def test_requires_name_and_type(self, patched_driver):
        result = await memory_tools.create_entity(
            "brand", "", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"
        result = await memory_tools.create_entity(
            "", "Mellanni", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_canonicalizes_type_on_write(self, patched_driver):
        # Simulate identified caller + no dupes + successful create.
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "admin@example.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_admin",
                    "first_name": "Admin",
                    "last_name": "User",
                    "full_name": "Admin User",
                    "is_auto_provisioned": False,
                })
            elif _is_entity_dedup_query(query):
                async def aiter(self):
                    if False:
                        yield None
                r.__aiter__ = aiter
                r.single = AsyncMock(return_value=None)
            else:
                r.single = AsyncMock(return_value={"entity_id": "ent_test"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_entity(
            "Brand", "Mellanni", description="Home textiles brand",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        assert result["entity_type"] == "brand"  # canonicalized from "Brand"
        # CREATE query should carry the canonicalized type parameter.
        create_call = next(
            c for c in patched_driver.run.await_args_list
            if "CREATE (e:Entity" in c.args[0]
        )
        assert create_call.kwargs["entity_type"] == "brand"

    @pytest.mark.asyncio
    async def test_returns_possible_duplicate_on_hit(self, patched_driver):
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "admin@example.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_admin", "first_name": "A", "last_name": "U",
                    "full_name": "A U", "is_auto_provisioned": False,
                })
            elif _is_entity_dedup_query(query):
                async def aiter(self):
                    yield {
                        "entity_id": "ent_existing",
                        "name": "Mellanni",
                        "entity_type": "brand",
                        "description_preview": "",
                        "created_at": "2026-01-01",
                        "score": 0.95,
                    }
                r.__aiter__ = aiter
                r.single = AsyncMock(return_value=None)
            else:
                r.single = AsyncMock(return_value={"entity_id": "ent_should_not_create"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_entity(
            "brand", "Mellanni Inc",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "possible_duplicate"
        assert len(result["matches"]) == 1
        assert result["matches"][0]["entity_id"] == "ent_existing"
        assert "force_create=true" in result["message"]


class TestSearchEntities:
    @pytest.mark.asyncio
    async def test_requires_query(self, patched_driver):
        result = await memory_tools.search_entities(
            "", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_type_filter_canonicalized(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            async def aiter(self):
                yield {
                    "entity_id": "ent_x", "name": "Mellanni", "entity_type": "brand",
                    "description_preview": "", "tags": [], "created_at": "2026-01-01",
                    "score": 0.88,
                }
            r.__aiter__ = aiter
            r.single = AsyncMock(return_value=None)
            return r

        patched_driver.run = AsyncMock(side_effect=_run)
        result = await memory_tools.search_entities(
            "Mellanni", entity_type="Brand",  # non-canonical input
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"
        assert result["entity_type_filter"] == "brand"
        call = patched_driver.run.await_args_list[0]
        assert call.kwargs["canonical_type"] == "brand"


class TestUpdateEntity:
    @pytest.mark.asyncio
    async def test_author_path_success(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={"entity_id": "ent_x"},
            resolve_hit={"canonical": "bob@example.com"},
        )
        result = await memory_tools.update_entity(
            "ent_x", json.dumps({"name": "New Name"}),
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"
        # Gate uses author_user_id predicate.
        upd_q = next(
            c.args[0] for c in patched_driver.run.await_args_list
            if "MATCH (e:Entity" in c.args[0] and "SET " in c.args[0]
        )
        assert "e.author_user_id = $caller_id" in upd_q
        # Name change should trigger embedding refresh.
        assert "genai.vector.encode" in upd_q

    @pytest.mark.asyncio
    async def test_rejects_unknown_fields(self, patched_driver):
        result = await memory_tools.update_entity(
            "ent_x", json.dumps({"weird": "value"}),
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "error"
        assert "weird" in result["message"]

    @pytest.mark.asyncio
    async def test_canonicalizes_type_on_update(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={"entity_id": "ent_x"},
            resolve_hit={"canonical": "bob@example.com"},
        )
        result = await memory_tools.update_entity(
            "ent_x", json.dumps({"entity_type": "Department"}),
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"
        # The parameter carried to the SET clause should be canonicalized.
        upd_call = next(
            c for c in patched_driver.run.await_args_list
            if "SET " in c.args[0] and "MATCH (e:Entity" in c.args[0]
        )
        assert upd_call.kwargs["entity_type"] == "department"

    @pytest.mark.asyncio
    async def test_non_author_forbidden(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row=None,  # MERGE/SET returns nothing → author gate failed
            resolve_hit={"canonical": "bob@example.com"},
        )
        result = await memory_tools.update_entity(
            "ent_x", json.dumps({"name": "new"}),
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"
        assert "not authored by you" in result["message"]


class TestDeleteEntity:
    @pytest.mark.asyncio
    async def test_admin_plain_match(self, patched_driver):
        async def _run(*args, **kwargs):
            r = AsyncMock()
            r.single = AsyncMock(return_value={"deleted": 1})
            return r
        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.delete_entity(
            "ent_x", tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        delete_q = patched_driver.run.await_args_list[0].args[0]
        assert "author_user_id" not in delete_q
        assert "DETACH DELETE e" in delete_q

    @pytest.mark.asyncio
    async def test_author_path_gated(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={"deleted": 1},
            resolve_hit={"canonical": "bob@example.com"},
        )
        result = await memory_tools.delete_entity(
            "ent_x", tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"
        del_q = next(
            q for q in (c.args[0] for c in patched_driver.run.await_args_list)
            if "DETACH DELETE e" in q
        )
        assert "e.author_user_id = $caller_id" in del_q


class TestRelateEntities:
    @pytest.mark.asyncio
    async def test_rejects_self_link(self, patched_driver):
        result = await memory_tools.relate_entities(
            "ent_a", "ent_a", "part_of",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_admin_path_success(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={
                "from_id": "ent_a", "to_id": "ent_b",
                "relation_type": "PART_OF", "outcome": "created",
            },
            resolve_hit={"canonical": "admin@example.com"},
        )
        result = await memory_tools.relate_entities(
            "ent_a", "ent_b", "Part Of",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        assert result["relation_type"] == "PART_OF"
        merge_q = next(
            q for q in (c.args[0] for c in patched_driver.run.await_args_list)
            if "MERGE (a)-[r:PART_OF]->(b)" in q
        )
        # Admin path has no MATCH-level author gate.
        assert "a.author_user_id" not in merge_q


class TestRelatePersonToEntity:
    @pytest.mark.asyncio
    async def test_requires_both_ids(self, patched_driver):
        result = await memory_tools.relate_person_to_entity(
            "", "ent_a", "owns",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_admin_path_success(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={
                "from_id": "per_igor", "to_id": "ent_poluco",
                "relation_type": "OWNS", "outcome": "created",
            },
            resolve_hit={"canonical": "admin@example.com"},
        )
        result = await memory_tools.relate_person_to_entity(
            "per_igor", "ent_poluco", "owns",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        assert result["from_person_id"] == "per_igor"
        assert result["to_entity_id"] == "ent_poluco"
        assert result["relation_type"] == "OWNS"
        # Query must match Person from, Entity to.
        merge_q = next(
            q for q in (c.args[0] for c in patched_driver.run.await_args_list)
            if "MERGE (a)-[r:OWNS]->(b)" in q
        )
        assert "MATCH (a:Person" in merge_q
        assert "(b:Entity" in merge_q

    @pytest.mark.asyncio
    async def test_author_path_gated(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={
                "from_id": "per_x", "to_id": "ent_y",
                "relation_type": "WORKS_AT", "outcome": "existing",
            },
            resolve_hit={"canonical": "bob@example.com"},
        )
        result = await memory_tools.relate_person_to_entity(
            "per_x", "ent_y", "works_at",
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"
        merge_q = next(
            q for q in (c.args[0] for c in patched_driver.run.await_args_list)
            if "MERGE (a)-[r:WORKS_AT]->(b)" in q
        )
        assert "a.author_user_id = $caller" in merge_q

    @pytest.mark.asyncio
    async def test_non_author_forbidden(self, patched_driver):
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "bob@example.com"})
            elif "MERGE (a)-[r:" in query:
                r.single = AsyncMock(return_value=None)
            elif "a.author_user_id AS author_user_id" in query:
                r.single = AsyncMock(
                    return_value={"author_user_id": "someone_else@example.com"}
                )
            else:
                r.single = AsyncMock(return_value=None)
            return r

        patched_driver.run = AsyncMock(side_effect=_run)
        result = await memory_tools.relate_person_to_entity(
            "per_x", "ent_y", "owns",
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "forbidden"
        assert "not authored by you" in result["message"]


class TestRelateEntityToPerson:
    @pytest.mark.asyncio
    async def test_admin_path_success(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={
                "from_id": "ent_amz", "to_id": "per_sergey",
                "relation_type": "LED_BY", "outcome": "created",
            },
            resolve_hit={"canonical": "admin@example.com"},
        )
        result = await memory_tools.relate_entity_to_person(
            "ent_amz", "per_sergey", "led by",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        assert result["from_entity_id"] == "ent_amz"
        assert result["to_person_id"] == "per_sergey"
        assert result["relation_type"] == "LED_BY"
        merge_q = next(
            q for q in (c.args[0] for c in patched_driver.run.await_args_list)
            if "MERGE (a)-[r:LED_BY]->(b)" in q
        )
        assert "MATCH (a:Entity" in merge_q
        assert "(b:Person" in merge_q


# ---------------------------------------------------------------------------
# _normalize_related + _group_by_relation (pure)
# ---------------------------------------------------------------------------


class TestNormalizeRelated:
    def test_bare_string_list_gets_default(self):
        out = memory_tools._normalize_related(
            ["per_1", "per_2"], "INVOLVES", ("person_id", "id"),
        )
        assert out == [("per_1", "INVOLVES"), ("per_2", "INVOLVES")]

    def test_dict_with_relation_type(self):
        out = memory_tools._normalize_related(
            [{"person_id": "per_1", "relation_type": "raised_by"}],
            "INVOLVES", ("person_id", "id"),
        )
        assert out == [("per_1", "RAISED_BY")]

    def test_dict_without_relation_type_falls_back(self):
        out = memory_tools._normalize_related(
            [{"person_id": "per_1"}], "INVOLVES", ("person_id", "id"),
        )
        assert out == [("per_1", "INVOLVES")]

    def test_mixed_bare_and_typed(self):
        out = memory_tools._normalize_related(
            ["per_1", {"person_id": "per_2", "relation_type": "decided_by"}],
            "INVOLVES", ("person_id", "id"),
        )
        assert out == [("per_1", "INVOLVES"), ("per_2", "DECIDED_BY")]

    def test_multiple_id_keys(self):
        # Memory-side items can use memory_id OR record_id OR id.
        out = memory_tools._normalize_related(
            [{"memory_id": "mem_1"}, {"record_id": "mem_2"}, {"id": "mem_3"}],
            "RELATED_TO", ("memory_id", "record_id", "id"),
        )
        assert out == [
            ("mem_1", "RELATED_TO"),
            ("mem_2", "RELATED_TO"),
            ("mem_3", "RELATED_TO"),
        ]

    def test_invalid_items_dropped(self):
        out = memory_tools._normalize_related(
            [{}, {"relation_type": "no_id"}, None, 42, ""],
            "INVOLVES", ("person_id", "id"),
        )
        assert out == []

    def test_group_by_relation_collapses_common(self):
        groups = memory_tools._group_by_relation(
            [("per_1", "INVOLVES"), ("per_2", "INVOLVES"), ("per_3", "RAISED_BY")]
        )
        assert groups == {"INVOLVES": ["per_1", "per_2"], "RAISED_BY": ["per_3"]}


# ---------------------------------------------------------------------------
# create_record with typed related_people (→ non-default edge type)
# ---------------------------------------------------------------------------


class TestCreateRecordTypedRelations:
    @pytest.mark.asyncio
    async def test_typed_related_people_creates_typed_edge(self, patched_driver):
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "admin@example.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_admin", "first_name": "A", "last_name": "U",
                    "full_name": "A U", "is_auto_provisioned": False,
                })
            elif _is_memory_dedup_query(query):
                async def aiter(self):
                    if False:
                        yield None
                r.__aiter__ = aiter
                r.single = AsyncMock(return_value=None)
            else:
                r.single = AsyncMock(return_value={"record_id": "mem_typed"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)
        result = await memory_tools.create_record(
            "professional", "Incident raised by Igor", "incident note",
            "incident", [],
            related_people=[{"person_id": "per_igor", "relation_type": "raised_by"}],
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        queries = [c.args[0] for c in patched_driver.run.await_args_list]
        # The typed edge — :RAISED_BY, not :INVOLVES — must be emitted.
        assert any("MERGE (m)-[r:RAISED_BY]->(p)" in q for q in queries), (
            "expected typed :RAISED_BY edge from dict form of related_people"
        )
        # And no :INVOLVES for this record, since all items were typed.
        assert not any("MERGE (m)-[r:INVOLVES]->(p)" in q for q in queries)

    @pytest.mark.asyncio
    async def test_bare_related_people_still_default_involves(self, patched_driver):
        """Backward-compat: bare string IDs keep the old :INVOLVES behaviour."""
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "admin@example.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_admin", "first_name": "A", "last_name": "U",
                    "full_name": "A U", "is_auto_provisioned": False,
                })
            elif _is_memory_dedup_query(query):
                async def aiter(self):
                    if False:
                        yield None
                r.__aiter__ = aiter
                r.single = AsyncMock(return_value=None)
            else:
                r.single = AsyncMock(return_value={"record_id": "mem_bare"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)
        result = await memory_tools.create_record(
            "professional", "Meeting involved Alice", "meeting note",
            "memory", [],
            related_people=["per_alice"],
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        queries = [c.args[0] for c in patched_driver.run.await_args_list]
        assert any("MERGE (m)-[r:INVOLVES]->(p)" in q for q in queries)


# ---------------------------------------------------------------------------
# relate_memory_to_person / relate_memory_to_entity / relate_memories
# ---------------------------------------------------------------------------


class TestRelateMemoryTools:
    @pytest.mark.asyncio
    async def test_memory_to_person_admin(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={
                "from_id": "mem_x", "to_id": "per_igor",
                "relation_type": "RAISED_BY", "outcome": "created",
            },
            resolve_hit={"canonical": "admin@example.com"},
        )
        result = await memory_tools.relate_memory_to_person(
            "mem_x", "per_igor", "raised_by",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        assert result["from_memory_id"] == "mem_x"
        assert result["to_person_id"] == "per_igor"
        assert result["relation_type"] == "RAISED_BY"
        merge_q = next(
            q for q in (c.args[0] for c in patched_driver.run.await_args_list)
            if "MERGE (a)-[r:RAISED_BY]->(b)" in q
        )
        assert "MATCH (a:Memory" in merge_q
        assert "(b:Person" in merge_q

    @pytest.mark.asyncio
    async def test_memory_to_entity_author_gated(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={
                "from_id": "mem_x", "to_id": "ent_mellanni",
                "relation_type": "AFFECTED", "outcome": "created",
            },
            resolve_hit={"canonical": "bob@example.com"},
        )
        result = await memory_tools.relate_memory_to_entity(
            "mem_x", "ent_mellanni", "affected",
            tool_context=_make_ctx("bob@example.com"),
        )
        assert result["status"] == "success"
        merge_q = next(
            q for q in (c.args[0] for c in patched_driver.run.await_args_list)
            if "MERGE (a)-[r:AFFECTED]->(b)" in q
        )
        # Author path must include the author_user_id gate.
        assert "a.author_user_id = $caller" in merge_q

    @pytest.mark.asyncio
    async def test_memories_rejects_self_link(self, patched_driver):
        result = await memory_tools.relate_memories(
            "mem_x", "mem_x", "supersedes",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_memories_typed_edge(self, patched_driver):
        patched_driver.run = _dispatched_run(
            record_row={
                "from_id": "mem_new", "to_id": "mem_old",
                "relation_type": "SUPERSEDES", "outcome": "created",
            },
            resolve_hit={"canonical": "admin@example.com"},
        )
        result = await memory_tools.relate_memories(
            "mem_new", "mem_old", "supersedes",
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        assert result["relation_type"] == "SUPERSEDES"
        merge_q = next(
            q for q in (c.args[0] for c in patched_driver.run.await_args_list)
            if "MERGE (a)-[r:SUPERSEDES]->(b)" in q
        )
        # Both source and target are :Memory.
        assert "MATCH (a:Memory" in merge_q
        assert "(b:Memory" in merge_q


# ---------------------------------------------------------------------------
# create_record with related_entities → :ABOUT edge
# ---------------------------------------------------------------------------


class TestCreateRecordEntityLink:
    @pytest.mark.asyncio
    async def test_related_entities_creates_about_edges(self, patched_driver):
        async def _run(*args, **kwargs):
            query = args[0] if args else ""
            r = AsyncMock()
            if _is_resolve_caller_lookup(query):
                r.single = AsyncMock(return_value={"canonical": "admin@example.com"})
            elif "p.is_auto_provisioned AS is_auto_provisioned" in query:
                r.single = AsyncMock(return_value={
                    "person_id": "per_admin", "first_name": "A", "last_name": "U",
                    "full_name": "A U", "is_auto_provisioned": False,
                })
            elif _is_memory_dedup_query(query):
                async def aiter(self):
                    if False:
                        yield None
                r.__aiter__ = aiter
                r.single = AsyncMock(return_value=None)
            else:
                r.single = AsyncMock(return_value={"record_id": "mem_new"})
            return r

        patched_driver.run = AsyncMock(side_effect=_run)

        result = await memory_tools.create_record(
            "professional", "About Mellanni brand", "mellanni note",
            "knowledge", [],
            related_entities=["ent_mellanni"],
            tool_context=_make_ctx("admin@example.com"),
        )
        assert result["status"] == "success"
        # Verify the :ABOUT link query fired.
        queries = [c.args[0] for c in patched_driver.run.await_args_list]
        assert any("MERGE (m)-[r:ABOUT]->(e)" in q for q in queries), (
            "expected a (:Memory)-[:ABOUT]->(:Entity) link after create_record with related_entities"
        )
