import os
import httpx
import asyncio

async def send():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        with open("ping_result.txt", "w") as f: f.write("Error: TELEGRAM_BOT_TOKEN not found in environment.")
        return
    
    chat_id = "185625742"
    text = "Hi! Sergey asked me to ping you and say hello!"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": text})
            with open("ping_result.txt", "w") as f: f.write(f"Status: {resp.status_code}\nResponse: {resp.text}")
    except Exception as e:
        with open("ping_result.txt", "w") as f: f.write(f"Exception: {str(e)}")

# Immediate execution on import
print("Starting ping...")
try:
    asyncio.run(send())
    print("Finished ping attempt.")
except Exception as e:
    with open("ping_result.txt", "w") as f: f.write(f"Import execution error: {str(e)}")
