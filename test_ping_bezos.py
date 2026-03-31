import asyncio
import httpx
import uuid
import sys

async def main(api_key):
    endpoint_url = "https://ips-vol-ware-features.trycloudflare.com"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-a2a-api-key"] = api_key

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"text": "Hello Bezos, are you there?"}],
            }
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            resp = await client.post(endpoint_url, json=payload, headers=headers)
            print(f"Status: {resp.status_code}")
            print(f"Body: {resp.text}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    asyncio.run(main(key))
