"""OriMemoryService — round-trip + scope filter.

Marked `infra` because FastEmbed downloads BAAI/bge-small-en-v1.5 on first
use, which the default pytest sandbox blocks (HTTP guardrail in conftest).
Run explicitly with `pytest -m infra` to exercise these.
"""

import pytest

from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from app.runtime.memory_service import OriMemoryService

pytestmark = pytest.mark.infra


@pytest.fixture
def svc(tmp_path):
    return OriMemoryService(db_path=str(tmp_path / "memdb"))


@pytest.mark.asyncio
async def test_add_and_search_roundtrip(svc):
    """Add a memory, search for similar text, get it back."""
    entry = MemoryEntry(
        content=types.Content(role="user", parts=[
            types.Part.from_text(text="user prefers concise answers")
        ]),
        custom_metadata={"category": "human_preferences"},
    )
    await svc.add_memory(
        app_name="ori", user_id="tg_42", memories=[entry],
        custom_metadata={"category": "human_preferences"},
    )
    resp = await svc.search_memory(app_name="ori", user_id="tg_42", query="concise")
    assert len(resp.memories) >= 1
    text = " ".join(p.text for p in resp.memories[0].content.parts if p.text)
    assert "concise" in text.lower()


@pytest.mark.asyncio
async def test_user_isolation(svc):
    """Memories for tg_42 don't leak to tg_99."""
    e = MemoryEntry(
        content=types.Content(role="user", parts=[
            types.Part.from_text(text="my secret coffee order is double espresso")
        ]),
    )
    await svc.add_memory(app_name="ori", user_id="tg_42", memories=[e])
    other = await svc.search_memory(app_name="ori", user_id="tg_99", query="coffee")
    assert other.memories == []


@pytest.mark.asyncio
async def test_empty_query_returns_empty(svc):
    resp = await svc.search_memory(app_name="ori", user_id="tg_1", query="")
    assert resp.memories == []
