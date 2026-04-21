"""Tests for the Neo4j knowledge graph module and tools."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core import graph
from app.tools import graph_tools


def _make_ctx(user_id="test_user@example.com"):
    ctx = MagicMock()
    state = {"user_id": user_id}
    ctx.state.to_dict.return_value = state
    return ctx


# ---------------------------------------------------------------------------
# graph.py — core Neo4j operations (mocked driver)
# ---------------------------------------------------------------------------

class TestGraphClient:
    """Tests for app/core/graph.py with mocked Neo4j driver."""

    @pytest.fixture(autouse=True)
    def reset_driver(self):
        """Reset global driver between tests."""
        graph._driver = None
        yield
        graph._driver = None

    def test_is_configured_false_by_default(self, monkeypatch):
        monkeypatch.delenv("NEO4J_URI", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        assert graph.is_configured() is False

    def test_is_configured_true(self, monkeypatch):
        monkeypatch.setenv("NEO4J_URI", "neo4j+s://test.neo4j.io")
        monkeypatch.setenv("NEO4J_PASSWORD", "secret")
        assert graph.is_configured() is True

    @pytest.mark.asyncio
    async def test_upsert_entity_not_configured(self, monkeypatch):
        monkeypatch.delenv("NEO4J_URI", raising=False)
        monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
        result = await graph.upsert_entity("ent_1", "Test", "concept")
        assert result["status"] == "error"
        assert "not configured" in result["message"]

    @pytest.mark.asyncio
    async def test_upsert_entity_success(self, monkeypatch):
        monkeypatch.setenv("NEO4J_URI", "neo4j+s://test.neo4j.io")
        monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
        monkeypatch.setenv("NEO4J_PASSWORD", "secret")

        mock_record = {"entity_id": "ent_1", "name": "Test Entity"}
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value=mock_record)

        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session = MagicMock(return_value=mock_session)

        with patch("app.core.graph.AsyncGraphDatabase.driver", return_value=mock_driver):
            result = await graph.upsert_entity(
                "ent_1", "Test Entity", "concept", author="test_user"
            )

        assert result["status"] == "success"
        assert result["entity_id"] == "ent_1"
        assert result["name"] == "Test Entity"

    @pytest.mark.asyncio
    async def test_get_entity_not_found(self, monkeypatch):
        monkeypatch.setenv("NEO4J_URI", "neo4j+s://test.neo4j.io")
        monkeypatch.setenv("NEO4J_PASSWORD", "secret")

        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value=None)

        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session = MagicMock(return_value=mock_session)

        with patch("app.core.graph.AsyncGraphDatabase.driver", return_value=mock_driver):
            result = await graph.get_entity("nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_add_relationship_sanitizes_type(self, monkeypatch):
        monkeypatch.setenv("NEO4J_URI", "neo4j+s://test.neo4j.io")
        monkeypatch.setenv("NEO4J_PASSWORD", "secret")

        mock_record = {"from_name": "A", "to_name": "B", "rel_type": "WORKS_WITH"}
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value=mock_record)

        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session = MagicMock(return_value=mock_session)

        with patch("app.core.graph.AsyncGraphDatabase.driver", return_value=mock_driver):
            result = await graph.add_relationship("ent_1", "ent_2", "works with")

        assert result["status"] == "success"
        assert result["relationship"] == "WORKS_WITH"

    @pytest.mark.asyncio
    async def test_add_relationship_invalid_type(self, monkeypatch):
        monkeypatch.setenv("NEO4J_URI", "neo4j+s://test.neo4j.io")
        monkeypatch.setenv("NEO4J_PASSWORD", "secret")

        mock_driver = MagicMock()
        with patch("app.core.graph.AsyncGraphDatabase.driver", return_value=mock_driver):
            result = await graph.add_relationship("ent_1", "ent_2", "!!!!")

        assert result["status"] == "error"
        assert "Invalid" in result["message"]

    @pytest.mark.asyncio
    async def test_search_entities_success(self, monkeypatch):
        monkeypatch.setenv("NEO4J_URI", "neo4j+s://test.neo4j.io")
        monkeypatch.setenv("NEO4J_PASSWORD", "secret")

        # Mock async iterator for result records
        records = [
            {"entity_id": "ent_1", "name": "Alice", "entity_type": "person",
             "pinecone_id": None, "author": "user"},
        ]

        # Create a proper async iterator
        async def mock_aiter(self):
            for r in records:
                yield r

        mock_result = AsyncMock()
        mock_result.__aiter__ = mock_aiter

        mock_session = AsyncMock()
        mock_session.run = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_driver = MagicMock()
        mock_driver.session = MagicMock(return_value=mock_session)

        with patch("app.core.graph.AsyncGraphDatabase.driver", return_value=mock_driver):
            result = await graph.search_entities("Alice")

        assert result["status"] == "success"
        assert result["count"] == 1
        assert result["entities"][0]["name"] == "Alice"


# ---------------------------------------------------------------------------
# graph_tools.py — ADK tool functions
# ---------------------------------------------------------------------------

class TestGraphTools:
    """Tests for app/tools/graph_tools.py."""

    @pytest.mark.asyncio
    async def test_add_entity_validates_type(self):
        ctx = _make_ctx()
        result = await graph_tools.add_entity("Test", "invalid_type", tool_context=ctx)
        assert result["status"] == "error"
        assert "entity_type" in result["message"]

    @pytest.mark.asyncio
    async def test_add_entity_requires_name(self):
        ctx = _make_ctx()
        result = await graph_tools.add_entity("", "person", tool_context=ctx)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_add_entity_invalid_properties_json(self):
        ctx = _make_ctx()
        result = await graph_tools.add_entity(
            "Test", "person", properties="not json", tool_context=ctx
        )
        assert result["status"] == "error"
        assert "JSON" in result["message"]

    @pytest.mark.asyncio
    @patch("app.tools.graph_tools.graph.upsert_entity", new_callable=AsyncMock)
    async def test_add_entity_success(self, mock_upsert):
        mock_upsert.return_value = {"status": "success", "entity_id": "ent_123", "name": "Alice"}
        ctx = _make_ctx()
        result = await graph_tools.add_entity("Alice", "person", tool_context=ctx)
        assert result["status"] == "success"
        mock_upsert.assert_called_once()
        call_kwargs = mock_upsert.call_args[1]
        assert call_kwargs["name"] == "Alice"
        assert call_kwargs["entity_type"] == "person"
        assert call_kwargs["author"] == "test_user@example.com"

    @pytest.mark.asyncio
    async def test_link_entities_requires_all_params(self):
        ctx = _make_ctx()
        result = await graph_tools.link_entities("", "ent_2", "WORKS_WITH", tool_context=ctx)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    @patch("app.tools.graph_tools._resolve_entity", new_callable=AsyncMock)
    async def test_link_entities_entity_not_found(self, mock_resolve):
        mock_resolve.return_value = None
        ctx = _make_ctx()
        result = await graph_tools.link_entities("Alice", "Bob", "WORKS_WITH", tool_context=ctx)
        assert result["status"] == "error"
        assert "not found" in result["message"]

    @pytest.mark.asyncio
    @patch("app.tools.graph_tools.graph.add_relationship", new_callable=AsyncMock)
    @patch("app.tools.graph_tools._resolve_entity", new_callable=AsyncMock)
    async def test_link_entities_success(self, mock_resolve, mock_add_rel):
        mock_resolve.side_effect = ["ent_1", "ent_2"]
        mock_add_rel.return_value = {
            "status": "success", "from": "Alice", "to": "Bob", "relationship": "WORKS_WITH"
        }
        ctx = _make_ctx()
        result = await graph_tools.link_entities("Alice", "Bob", "WORKS_WITH", tool_context=ctx)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    @patch("app.tools.graph_tools._resolve_entity", new_callable=AsyncMock)
    @patch("app.tools.graph_tools.graph.get_connections", new_callable=AsyncMock)
    async def test_query_connections(self, mock_get_conn, mock_resolve):
        mock_resolve.return_value = "ent_1"
        mock_get_conn.return_value = {"status": "success", "count": 0, "connections": []}
        ctx = _make_ctx()
        result = await graph_tools.query_connections("Alice", tool_context=ctx)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    @patch("app.tools.graph_tools.graph.search_entities", new_callable=AsyncMock)
    async def test_search_graph(self, mock_search):
        mock_search.return_value = {"status": "success", "count": 1, "entities": [{"name": "Alice"}]}
        ctx = _make_ctx()
        result = await graph_tools.search_graph("Alice", tool_context=ctx)
        assert result["status"] == "success"
        assert result["count"] == 1

