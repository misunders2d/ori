import asyncio
from google.adk.agents import Agent
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from starlette.testclient import TestClient

async def main():
    agent = Agent(name="test", instruction="test")
    app = to_a2a(agent)
    
    with TestClient(app) as client:
        for route in app.routes:
            print(getattr(route, "path", route), getattr(route, "methods", None))

if __name__ == "__main__":
    asyncio.run(main())
