import pytest
import asyncio
import os
import shutil
import importlib
from unittest.mock import MagicMock, patch

# Mock class to simulate TextEmbedding without triggering huggingface_hub
class MockEmbeddingResult:
    def __init__(self, vector):
        self.vector = vector
    def tolist(self):
        return self.vector

class MockTextEmbedding:
    def __init__(self, model_name=None):
        pass
    def embed(self, texts):
        # Return a dummy vector of length 384 (standard for bge-small-en-v1.5)
        for _ in texts:
            yield MockEmbeddingResult([0.1] * 384)

@pytest.fixture(scope="module")
def memory_system():
    # Use a temporary directory for the test DB
    test_db_path = os.path.abspath("./data/test_memory_db")
    if os.path.exists(test_db_path):
        shutil.rmtree(test_db_path)
    
    # Reload the memory module to ensure we're testing the sandbox staged code
    import app.core.memory
    importlib.reload(app.core.memory)
    
    # Patch TextEmbedding BEFORE initializing LongTermMemory
    with patch("app.core.memory.TextEmbedding", MockTextEmbedding):
        # Also mock the DB_PATH
        original_path = app.core.memory.DB_PATH
        app.core.memory.DB_PATH = test_db_path
        
        # Instantiate
        from app.core.memory import LongTermMemory
        mem = LongTermMemory()
        
        yield mem
        
        # Cleanup
        if os.path.exists(test_db_path):
            shutil.rmtree(test_db_path)
        app.core.memory.DB_PATH = original_path

@pytest.mark.asyncio
async def test_memory_lifecycle(memory_system):
    category = "test_cat"
    content = "The secret password is 'evolution'."
    
    # 1. Create
    # Ensure memory_system.remember exists and returns a string
    record_id = await memory_system.remember(category, content, {"importance": 5})
    assert record_id is not None
    assert isinstance(record_id, str)
    
    # 2. Search
    results = await memory_system.search(category, "password")
    assert len(results) > 0
    # Search returns a list of dictionaries via LanceDB's .to_list()
    assert results[0]["text"] == content
    assert results[0]["id"] == record_id
    
    # 3. Update
    new_content = "The secret password has changed to 'synergy'."
    await memory_system.update(category, record_id, text=new_content)
    
    updated_results = await memory_system.search(category, "password")
    assert len(updated_results) > 0
    assert updated_results[0]["text"] == new_content
    assert updated_results[0]["id"] == record_id
    
    # 4. Delete
    await memory_system.forget(category, record_id)
    post_delete_results = await memory_system.search(category, "password")
    
    # Check if the specific ID is gone
    ids = [r["id"] for r in post_delete_results]
    assert record_id not in ids
