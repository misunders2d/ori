import pytest
import asyncio
from app.core.memory import memory

@pytest.mark.asyncio
async def test_debug_memory_schema():
    await memory.remember("debug_test", "This is a test record for schema debugging.")
    table = memory._get_table("debug_test")
    # This will fail and show us the schema if we look at the AssertionError
    assert False, f"Table Schema: {table.schema}"
