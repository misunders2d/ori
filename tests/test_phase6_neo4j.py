"""Phase 6 unit tests: pure-Python helpers for the Neo4j subsystem.

Avoids live Neo4j by exercising only the offline helpers — node-id
parsing, label mapping, audit-log writing, and the polymorphic dispatch
in `query_connections`. Cypher-touching code paths are smoke-tested via
patched driver returns.
"""

import json
import os

import pytest


# ---------------------------------------------------------------------------
# graph.py — pure helpers
# ---------------------------------------------------------------------------


def test_label_and_key_for_known_prefixes():
    from app.core.graph import _label_and_key_for

    assert _label_and_key_for("mem_2026_05_11_abcd1234") == ("Memory", "record_id")
    assert _label_and_key_for("per_2026_05_11_deadbeef") == ("Person", "person_id")
    assert _label_and_key_for("ent_2026_05_11_cafe5678") == ("Entity", "entity_id")


def test_label_and_key_for_unknown_returns_none():
    from app.core.graph import _label_and_key_for

    assert _label_and_key_for("Mellanni") is None  # bare name
    assert _label_and_key_for("") is None
    assert _label_and_key_for(None) is None  # type: ignore[arg-type]


def test_summarize_node_props_memory_shape():
    from app.core.graph import _summarize_node_props

    out = _summarize_node_props(
        {"record_id": "mem_abc", "short_description": "Q2 review", "text": "long"},
        ["Memory", "ProfessionalMemory"],
    )
    assert out["kind"] == "memory"
    assert out["id"] == "mem_abc"
    assert out["short_description"] == "Q2 review"
    assert out["labels"] == ["Memory", "ProfessionalMemory"]


def test_summarize_node_props_person_shape():
    from app.core.graph import _summarize_node_props

    out = _summarize_node_props(
        {"person_id": "per_alice", "first_name": "Alice", "last_name": "Smith"},
        ["Person", "ProfessionalPerson"],
    )
    assert out["kind"] == "person"
    assert out["id"] == "per_alice"
    assert out["full_name"] == "Alice Smith"


def test_summarize_node_props_entity_shape():
    from app.core.graph import _summarize_node_props

    out = _summarize_node_props(
        {"entity_id": "ent_mellanni", "name": "Mellanni", "entity_type": "brand"},
        ["Entity"],
    )
    assert out["kind"] == "entity"
    assert out["id"] == "ent_mellanni"
    assert out["name"] == "Mellanni"
    assert out["entity_type"] == "brand"


def test_summarize_node_props_empty_props_falls_back_to_unknown():
    from app.core.graph import _summarize_node_props

    out = _summarize_node_props({}, [])
    assert out["kind"] == "unknown"
    assert out["id"] == ""


# ---------------------------------------------------------------------------
# memory_tools.py — _audit helper
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_audit_log(tmp_path, monkeypatch):
    """Redirect _AUDIT_PATH to a tmp file so we can read what got written."""
    audit_file = tmp_path / "graph_audit.jsonl"
    from app.tools import memory_tools
    monkeypatch.setattr(memory_tools, "_AUDIT_PATH", str(audit_file))
    return audit_file


def test_audit_appends_one_jsonl_line(isolated_audit_log):
    from app.tools.memory_tools import _audit

    _audit("create_record", "user@x.com", "mem_abc", None, {"namespace": "personal"})

    lines = isolated_audit_log.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["op"] == "create_record"
    assert event["author_user_id"] == "user@x.com"
    assert event["node_id"] == "mem_abc"
    assert event["before"] is None
    assert event["after"] == {"namespace": "personal"}
    assert "ts" in event


def test_audit_appends_in_order(isolated_audit_log):
    from app.tools.memory_tools import _audit

    _audit("create_record", "u1", "mem_1", None, {"v": 1})
    _audit("delete_record", "u1", "mem_1", {"v": 1}, None)

    lines = isolated_audit_log.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["op"] == "create_record"
    assert json.loads(lines[1])["op"] == "delete_record"


def test_audit_swallows_write_errors(monkeypatch):
    """If the audit file can't be opened, the graph mutation must NOT fail."""
    from app.tools import memory_tools

    monkeypatch.setattr(
        memory_tools,
        "_AUDIT_PATH",
        "/proc/1/foreign/cannot-write/here/audit.jsonl",
    )

    # Should not raise — exception is logged and swallowed.
    memory_tools._audit("delete_record", "u", "mem_x", {"v": 1}, None)


# ---------------------------------------------------------------------------
# memory_tools.py — _labels_for_reembed
# ---------------------------------------------------------------------------


def test_labels_for_reembed_personal():
    from app.tools.memory_tools import _labels_for_reembed

    out = _labels_for_reembed("personal")
    labels = [t[0] for t in out]
    assert labels == ["PersonalMemory", "PersonalPerson"]


def test_labels_for_reembed_professional():
    from app.tools.memory_tools import _labels_for_reembed

    out = _labels_for_reembed("professional")
    labels = [t[0] for t in out]
    assert labels == ["ProfessionalMemory", "ProfessionalPerson"]


def test_labels_for_reembed_technical_has_no_person_scope():
    from app.tools.memory_tools import _labels_for_reembed

    out = _labels_for_reembed("technical")
    labels = [t[0] for t in out]
    assert labels == ["TechnicalMemory"]  # by design: no :TechnicalPerson label


def test_labels_for_reembed_entity_only():
    from app.tools.memory_tools import _labels_for_reembed

    out = _labels_for_reembed("entity")
    labels = [t[0] for t in out]
    assert labels == ["Entity"]


def test_labels_for_reembed_all_covers_six_labels():
    from app.tools.memory_tools import _labels_for_reembed

    out = _labels_for_reembed("all")
    assert len(out) == 6


def test_labels_for_reembed_unknown_returns_empty():
    from app.tools.memory_tools import _labels_for_reembed

    assert _labels_for_reembed("bogus") == []


# ---------------------------------------------------------------------------
# graph_tools.py — query_connections polymorphic dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_connections_dispatches_to_polymorphic(monkeypatch):
    """A mem_/per_/ent_ prefix should route to get_node_neighbors."""
    from app.core import graph as core_graph
    from app.tools import graph_tools

    captured = {}

    async def fake_neighbors(node_id, max_depth=2, limit=25):
        captured["node_id"] = node_id
        captured["max_depth"] = max_depth
        return {"status": "success", "connections": []}

    async def fake_entity(*args, **kwargs):  # should NOT be called
        captured["entity_path_called"] = True
        return {"status": "error", "message": "should not be reached"}

    monkeypatch.setattr(core_graph, "get_node_neighbors", fake_neighbors)
    monkeypatch.setattr(core_graph, "get_connections", fake_entity)
    monkeypatch.setattr(graph_tools.graph, "get_node_neighbors", fake_neighbors)
    monkeypatch.setattr(graph_tools.graph, "get_connections", fake_entity)

    result = await graph_tools.query_connections("mem_abc123", max_depth=3)

    assert captured == {"node_id": "mem_abc123", "max_depth": 3}
    assert "entity_path_called" not in captured
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_query_connections_falls_back_to_entity_for_bare_name(monkeypatch):
    """A bare name should resolve via _resolve_entity then hit get_connections."""
    from app.core import graph as core_graph
    from app.tools import graph_tools

    captured = {}

    async def fake_resolve(identifier):
        captured["resolved_from"] = identifier
        return "ent_resolved"

    async def fake_get_connections(entity_id, max_depth=2, limit=50):
        captured["entity_id"] = entity_id
        return {"status": "success", "connections": [], "via": "entity"}

    async def fake_neighbors(*args, **kwargs):  # should NOT be called
        captured["polymorphic_called"] = True
        return {}

    monkeypatch.setattr(graph_tools, "_resolve_entity", fake_resolve)
    monkeypatch.setattr(graph_tools.graph, "get_connections", fake_get_connections)
    monkeypatch.setattr(graph_tools.graph, "get_node_neighbors", fake_neighbors)
    monkeypatch.setattr(core_graph, "get_node_neighbors", fake_neighbors)

    result = await graph_tools.query_connections("Mellanni")

    assert captured["resolved_from"] == "Mellanni"
    assert captured["entity_id"] == "ent_resolved"
    assert "polymorphic_called" not in captured
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_find_connection_path_polymorphic_when_both_prefixed(monkeypatch):
    from app.tools import graph_tools

    captured = {}

    async def fake_find_node_path(from_id, to_id):
        captured["from_id"] = from_id
        captured["to_id"] = to_id
        return {"status": "success", "hops": 1, "nodes": [], "edges": []}

    monkeypatch.setattr(graph_tools.graph, "find_node_path", fake_find_node_path)

    result = await graph_tools.find_connection_path("mem_abc", "per_xyz")

    assert captured == {"from_id": "mem_abc", "to_id": "per_xyz"}
    assert result["status"] == "success"


# ---------------------------------------------------------------------------
# reembed_entities — admin gate + dry_run shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reembed_forbidden_for_non_admin(monkeypatch):
    from app.tools import memory_tools

    # Non-admin caller
    monkeypatch.setattr(memory_tools, "_get_caller", lambda ctx: "noone@example.com")
    monkeypatch.setattr(memory_tools, "_get_acl_flags", lambda caller: (False, False))

    result = await memory_tools.reembed_entities(scope="personal", dry_run=True)

    assert result["status"] == "forbidden"


@pytest.mark.asyncio
async def test_reembed_invalid_scope_errors(monkeypatch):
    from app.tools import memory_tools

    monkeypatch.setattr(memory_tools, "_get_caller", lambda ctx: "admin@x.com")
    monkeypatch.setattr(memory_tools, "_get_acl_flags", lambda c: (True, True))

    result = await memory_tools.reembed_entities(scope="bogus", dry_run=True)

    assert result["status"] == "error"
    assert "Invalid scope" in result["message"]
