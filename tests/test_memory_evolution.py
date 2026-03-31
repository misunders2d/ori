import pytest
import asyncio
import os
import shutil
from app.core.memory import memory, DB_PATH

@pytest.fixture(autouse=True)
def setup_teardown():
    # Cleanup memory DB before each test
    if os.path.exists(DB_PATH):
        shutil.rmtree(DB_PATH)
    yield

@pytest.mark.asyncio
async def test_memory_crud():
    # 1. Remember
    await memory.remember("technical_test", "Ori is a self-evolving agent.", {"importance": 5})
    
    # 2. Search
    results = await memory.search("technical_test", "Who is Ori?")
    assert len(results) > 0
    assert "self-evolving" in results[0]["text"]
    assert "id" in results[0]
    
    rid = results[0]["id"]
    
    # 3. Update
    # FIXED: Signature is (category, record_id, text=None, metadata=None)
    await memory.update("technical_test", rid, text="Ori is a digital organism.")
    
    # 4. Search again
    results2 = await memory.search("technical_test", "Who is Ori?")
    assert len(results2) > 0
    # Search is semantic, so it should still find it.
    assert "organism" in results2[0]["text"]
    
    # 5. Delete
    await memory.forget("technical_test", rid)
    results3 = await memory.search("technical_test", "Who is Ori?")
    # Check that "organism" is no longer present
    texts = [r["text"] for r in results3]
    assert "organism" not in texts
