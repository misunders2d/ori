import asyncio
from app.toolsets.keepa import KeepaToolset

async def main():
    ts = KeepaToolset()
    tools = await ts.get_tools()
    print(tools)

if __name__ == "__main__":
    asyncio.run(main())
