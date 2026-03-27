import asyncio
import os

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# Load environment variables from .env file if it exists
ENV_FILE_PATH = os.environ.get("DOTENV_PATH", "./data/.env")
os.makedirs(os.path.dirname(ENV_FILE_PATH), exist_ok=True)

if os.path.exists(ENV_FILE_PATH):
    load_dotenv(ENV_FILE_PATH, override=True)

def ensure_valid_api_key():
    api_key = os.environ.get("GOOGLE_API_KEY")
    while True:
        if not api_key:
            print("Welcome to the Autonomous Amazon Manager!")
            print("It looks like you haven't configured your Google Gemini API key yet.")
            print("You can get a free key from: https://aistudio.google.com/app/apikey")
            api_key = input("\nPlease paste your GOOGLE_API_KEY here: ").strip()

            if not api_key:
                print("No key provided. Please try again.")
                continue

            # Temporarily set it for validation
            os.environ["GOOGLE_API_KEY"] = api_key

        print("\nValidating API key...")
        try:
            from google.genai import Client
            client = Client()
            # Simple fast call to check validity
            client.models.get(model="gemini-3-flash-preview")
            print("✅ Key is valid!\n")

            # Now that it's valid, create the .env template if it doesn't exist
            if not os.path.exists(ENV_FILE_PATH):
                print(f"Creating configuration template at {ENV_FILE_PATH}...")
                with open(ENV_FILE_PATH, 'w') as f:
                    f.write(f"""# Google Gemini API Key
GOOGLE_API_KEY="{api_key}"
GOOGLE_GENAI_USE_VERTEXAI=False

# Slack Integration
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=

# Telegram Integration
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=

# Database URL for persistent sessions
DATABASE_URL=sqlite:///./data/ori.db
""")
            else:
                from dotenv import set_key
                set_key(ENV_FILE_PATH, "GOOGLE_API_KEY", api_key)

            break # Exit the loop, key is good

        except Exception as e:
            print(f"❌ Invalid API Key detected: {e}")
            print("Google rejected the key. Please provide a valid one.")
            # Clear the bad key to trigger the prompt again
            api_key = None
            os.environ.pop("GOOGLE_API_KEY", None)

ensure_valid_api_key()

os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "False")

# Import our root agent application
from app.agent import root_agent


async def main():
    """Runs the agent with a sample ASIN to test the delegation flow."""
    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name="ori", user_id="test_user", session_id="test_session"
    )

    runner = Runner(
        agent=root_agent,
        app_name="ori",
        session_service=session_service
    )

    query = "Please schedule a reminder to review my projects in 3 hours."
    print(f"User: {query}")
    print("-" * 40)

    async for event in runner.run_async(
        user_id="test_user",
        session_id="test_session",
        new_message=types.Content(
            role="user",
            parts=[types.Part.from_text(text=query)]
        ),
    ):
        if event.author != "user" and event.content and event.content.parts:
            # We don't have to print every intermediate thought, but it's good for debugging
            text = event.content.parts[0].text
            if text:
                print(f"[{event.author}]: {text}")

        # Check for tool calls to show delegation
        if event.get_function_calls():
            for call in event.get_function_calls():
                if call.name == "transfer_to_agent":
                    print(f"[{event.author}] => DELEGATING TO: {call.args.get('agent_name')}")
                else:
                    print(f"[{event.author}] -> Calling tool: {call.name}")

if __name__ == "__main__":
    asyncio.run(main())
