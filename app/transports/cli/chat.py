"""CLI fallback chat — terminal interaction when no messenger is configured.

Imports `app.runtime.executor` (built in Phase G); module-level imports
will fail until then. The chat loop itself is unchanged in spirit from
the legacy `interfaces/cli_chat.py` — only import paths updated.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime

from google.genai import types  # noqa: F401  (used by future image-relay paths)

from app.runtime.executor import _inject_metadata_header, extract_agent_response


def is_enabled() -> bool:
    """CLI is the fallback — runs only when no messenger token is present."""
    return not any(
        os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "SLACK_BOT_TOKEN")
    )


async def start_poller(get_runner_fn, _process_init_fn=None) -> None:
    """Public entry point for the transports REGISTRY (no process_init for CLI)."""
    return await start_cli_chat(get_runner_fn)


async def start_cli_chat(get_runner_fn):
    """Launch a basic text-only chat in the terminal (onboarding fallback)."""
    bot_name = os.environ.get("BOT_NAME", "Ori")
    user_id = "terminal_user"
    session_id = f"cli_onboarding_{int(time.time())}"

    print(f"\n[ {bot_name} Onboarding Console ]")
    print("-----------------------------------")
    print("Welcome! Since no messenger (like Telegram) is configured yet,")
    print("I've started this basic text chat to help you get set up.\n")

    onboarding_trigger = (
        "This is the very first time the user is interacting with you after installation. "
        "1. Ask the user which language they prefer to speak in.\n"
        "2. Briefly outline your core principles (Self-evolution, safety, and persistence).\n"
        "3. Explain that they need to configure a messenger (like Telegram) to use you fully.\n"
        "Keep it friendly and concise."
    )

    runner = get_runner_fn()
    if runner:
        print(f"{bot_name} is initializing...", end="\r", flush=True)
        response = await extract_agent_response(
            runner, user_id, session_id, onboarding_trigger
        )
        print(" " * 30, end="\r", flush=True)
        print(f"{bot_name}: {response.text}")
        await asyncio.sleep(1.0)
    else:
        print(
            f"{bot_name}: I'm not fully configured yet. I need a GOOGLE_API_KEY "
            f"to start the onboarding chat."
        )
        print(
            "Please enter your GOOGLE_API_KEY in the .env file or use the /init "
            "command if you have a messenger."
        )

    empty_reads = 0
    while True:
        runner = get_runner_fn()
        if not runner:
            print(f"{bot_name}: I'm not fully configured yet. Need a GOOGLE_API_KEY.")
            await asyncio.sleep(5)
            continue

        try:
            def get_user_input():
                # Read directly from the TTY device to bypass Docker/gosu
                # stdin detachment or EOF loops.
                if os.path.exists("/dev/tty"):
                    try:
                        with open("/dev/tty", "r") as tty:
                            print("You: ", end="", flush=True)
                            return tty.readline()
                    except Exception:
                        pass
                try:
                    return input("You: ")
                except EOFError:
                    return None
                except Exception as e:
                    import logging
                    logging.error("Input thread crashed: %s", e)
                    return None

            raw_input_ = await asyncio.to_thread(get_user_input)
            if raw_input_ is None or raw_input_ == "":
                empty_reads += 1
                if empty_reads > 5:
                    print("\nTerminal disconnected. Exiting CLI chat.")
                    break
                await asyncio.sleep(1)
                continue
            empty_reads = 0
            user_input = raw_input_.strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "/exit"):
                print(f"\n{bot_name}: Closing onboarding chat.")
                break

            # /models command — same behavior as the Telegram poller; helpers
            # land in Phase F.
            if user_input.strip().startswith("/models"):
                try:
                    from app.util.models import (  # type: ignore[attr-defined]
                        format_model_assignments,
                        reset_all_models,
                        reset_model,
                        list_components,
                    )
                except ImportError:
                    print("\n/models is not yet wired in this build.\n")
                    continue
                parts = user_input.strip().split()
                if len(parts) == 1:
                    print(f"\n{format_model_assignments(markdown=False)}\n")
                elif len(parts) >= 2 and parts[1].lower() == "default":
                    valid = set(list_components())
                    if len(parts) == 2:
                        cleared = reset_all_models()
                        print(
                            f"\nReset {len(cleared)} model override(s) to defaults.\n"
                            if cleared
                            else "\nNo overrides to reset — already on defaults.\n"
                        )
                        print(f"{format_model_assignments(markdown=False)}\n")
                    else:
                        component = parts[2]
                        if component not in valid:
                            print(
                                f"\nUnknown component '{component}'. "
                                f"Valid: {', '.join(sorted(valid))}\n"
                            )
                        else:
                            cleared = reset_model(component)
                            print(
                                f"\nReset {component} to default.\n"
                                if cleared
                                else f"\n{component} was already on its default.\n"
                            )
                else:
                    print(
                        "\nUsage:\n"
                        "  /models                     — list all agent model assignments\n"
                        "  /models default             — reset ALL to defaults\n"
                        "  /models default <Component> — reset one to default\n"
                    )
                continue

            # Secure key capture — intercept before reaching agent.
            from app.runtime.secure_capture import (
                capture_friend_key,
                capture_key,
                check_pending,
                check_pending_friend,
            )
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
            enriched_input = _inject_metadata_header(user_input, datetime.utcnow(), "cli")
            response = await extract_agent_response(
                runner, user_id, session_id, enriched_input
            )
            print(" " * 30, end="\r", flush=True)
            print(f"\n{bot_name}: {response.text}")

        except EOFError:
            break
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(2)

        # Clean shutdown signal.
        from app.tools.system import check_exit_signal, consume_exit_signal
        if check_exit_signal():
            if consume_exit_signal():
                print(f"\n{bot_name}: Shutting down cleanly...")
                break
