import asyncio
import os
import sys
from google.genai import types
from app.core.agent_executor import extract_agent_response

async def start_cli_chat(get_runner_fn):
    """
    Launches a basic text-only chat in the terminal.
    Used for onboarding when no other messaging platforms are configured.
    """
    bot_name = os.environ.get("BOT_NAME", "Ori")
    user_id = "terminal_user"
    session_id = "cli_onboarding"
    
    print(f"\n[ {bot_name} Onboarding Console ]")
    print("-----------------------------------")
    print("Welcome! Since no messenger (like Telegram) is configured yet,")
    print("I've started this basic text chat to help you get set up.\n")

    # Initial onboarding prompt injection
    onboarding_trigger = (
        "This is the very first time the user is interacting with you after installation. "
        "1. Ask the user which language they prefer to speak in.\n"
        "2. Briefly outline your core principles (Self-evolution, safety, and persistence).\n"
        "3. Explain that they need to configure a messenger (like Telegram) to use you fully.\n"
        "Keep it friendly and concise."
    )

    # Initial response trigger
    runner = get_runner_fn()
    if runner:
        print(f"{bot_name} is initializing...", end="\r", flush=True)
        response = await extract_agent_response(
            runner, user_id, session_id, onboarding_trigger
        )
        print(" " * 30, end="\r", flush=True)
        print(f"{bot_name}: {response.text}")
    else:
        print(f"{bot_name}: I'm not fully configured yet. I need a GOOGLE_API_KEY to start the onboarding chat.")
        print(f"Please enter your GOOGLE_API_KEY in the .env file or use the /init command if you have a messenger.")

    while True:
        runner = get_runner_fn()
        if not runner:
            print(f"{bot_name}: I'm not fully configured yet. I need a GOOGLE_API_KEY to start.")
            await asyncio.sleep(5)
            continue

        # Get user input
        try:
            # Use input() instead of sys.stdin.readline for better TTY handling
            # run_in_executor allows us to wait for input without blocking the event loop
            def get_user_input():
                return input("You: ")
                
            user_input = await asyncio.get_event_loop().run_in_executor(None, get_user_input)
            user_input = user_input.strip()
            
            if not user_input:
                # Add a small sleep to prevent spinning if stdin is acting up
                await asyncio.sleep(0.1)
                continue
                
            if user_input.lower() in ["exit", "quit", "/exit"]:
                print(f"\n{bot_name}: Closing onboarding chat. You can reach me via configured messengers!")
                break

            # SECURE KEY CAPTURE
            from app.secure_config import capture_key, check_pending, capture_friend_key, check_pending_friend
            
            if check_pending(session_id):
                result = capture_key(session_id, user_input)
                print(" " * 30, end="\r", flush=True)
                print(f"\n{bot_name}: {result['message']}")
                continue
                
            if check_pending_friend(session_id):
                result = capture_friend_key(session_id, user_input)
                print(" " * 30, end="\r", flush=True)
                print(f"\n{bot_name}: {result['message']}")
                continue

            print(f"\n{bot_name} is thinking...", end="\r", flush=True)
            
            response = await extract_agent_response(
                runner, user_id, session_id, user_input
            )
            
            # Clear "thinking" line
            print(" " * 30, end="\r", flush=True)
            print(f"\n{bot_name}: {response.text}")

        except EOFError:
            break
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(2)
