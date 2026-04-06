import asyncio
from app.tools.keepa_api import keepa_check_tokens
import logging

logging.basicConfig(level=logging.DEBUG)

async def main():
    res = await keepa_check_tokens()
    print("res:", res)

if __name__ == "__main__":
    asyncio.run(main())