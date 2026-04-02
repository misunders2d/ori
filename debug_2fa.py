import os
import asyncio
from unittest.mock import MagicMock, patch
from app.callbacks.guardrails import admin_tool_guardrail
from app.tools.system import execute_approved_action

class MockTool:
    def __init__(self, name):
        self.name = name

async def run_debug():
    tool = MockTool("update_self")
    context = MagicMock()
    context.state.to_dict.return_value = {
        "user_id": "admin_user",
        "session_id": "test_session"
    }

    print("Checking guardrail...")
    with patch.dict(os.environ, {"ADMIN_USER_IDS": "admin_user", "ADMIN_TOTP_SECRET": "base32secret3232", "REQUIRE_2FA": "false"}):
        with patch("app.core.pending_actions.stage_action", return_value="test_token"):
            result = admin_tool_guardrail(tool, {}, context)
            print(f"Result (REQUIRE_2FA=false): {result['message'][:50]}...")
            if "2FA code" not in result["message"]:
                print("PASS: 2FA code prompt absent when REQUIRE_2FA=false")
            else:
                print("FAIL: 2FA code prompt present when REQUIRE_2FA=false")

    print("\nChecking execute_approved_action...")
    with patch.dict(os.environ, {"ADMIN_TOTP_SECRET": "base32secret3232", "REQUIRE_2FA": "false"}):
        with patch("app.core.pending_actions.get_and_delete_action", return_value={"tool_name": "session_refresh", "args": {"mode": "soft"}}):
            with patch("app.tools.session_refresh", return_value={"status": "success"}):
                result = await execute_approved_action("test_token")
                print(f"Result: {result}")
                if result["status"] == "success":
                    print("PASS: Execution succeeded without TOTP code when REQUIRE_2FA=false")
                else:
                    print("FAIL: Execution failed without TOTP code when REQUIRE_2FA=false")

if __name__ == "__main__":
    asyncio.run(run_debug())
