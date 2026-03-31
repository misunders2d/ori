import asyncio
from app.core.memory import memory

async def main():
    await memory.remember("debug", "test text")
    table = memory._get_table("debug")
    print(f"Table Name: {table.name}")
    print(f"Schema: {table.schema}")
    results = await memory.search("debug", "test")
    print(f"Result: {results[0]}")
    if "id" in results[0]:
        print("ID found")
    else:
        print("ID NOT found")

if __name__ == "__main__":
    asyncio.run(main())
