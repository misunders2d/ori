import pytest
import importlib
from unittest.mock import patch

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


class FakeQuery:
    def __init__(self, table):
        self.table = table
        self._limit = 5

    def limit(self, limit):
        self._limit = limit
        return self

    def to_list(self):
        return [record.copy() for record in self.table.records[: self._limit]]


class FakeTable:
    def __init__(self, data=None):
        self.records = [record.copy() for record in (data or [])]

    def add(self, records):
        self.records.extend(record.copy() for record in records)

    def search(self, _query_vector):
        return FakeQuery(self)

    def update(self, where, values):
        record_id = where.split("'", 2)[1]
        for record in self.records:
            if record.get("id") == record_id:
                record.update(values)

    def delete(self, where):
        record_id = where.split("'", 2)[1]
        self.records = [
            record for record in self.records if record.get("id") != record_id
        ]


class FakeDB:
    def __init__(self):
        self.tables = {}

    def table_names(self):
        return list(self.tables)

    def open_table(self, table_name):
        return self.tables[table_name]

    def create_table(self, table_name, data):
        table = FakeTable(data)
        self.tables[table_name] = table
        return table


@pytest.fixture(scope="module")
def memory_system():
    # Reload the memory module to ensure we're testing the sandbox staged code
    import app.core.memory
    importlib.reload(app.core.memory)

    # Patch external embedding and DB layers so default tests never download
    # models or depend on LanceDB native runtime behavior.
    with (
        patch("app.core.memory.TextEmbedding", MockTextEmbedding),
        patch("app.core.memory.lancedb.connect", return_value=FakeDB()),
    ):
        # Instantiate
        from app.core.memory import LongTermMemory
        mem = LongTermMemory()
        yield mem

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
