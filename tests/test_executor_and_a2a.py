"""Phase G smoke — executor + A2A wiring.

Runtime LLM-driven tests belong in eval; here we only verify the surface
stitches together: AgentResponse shape, metadata header, agent card
multimodal modes, OAuth callback registration."""

from datetime import datetime, timezone

from app.a2a_server import (
    _build_agent_card,
    register_pending_oauth,
    _consume_pending_oauth,
)
from app.runtime.executor import AgentResponse, _inject_metadata_header


# ---------------------------------------------------------------------------
# AgentResponse
# ---------------------------------------------------------------------------

def test_agent_response_str_contract():
    r = AgentResponse(text="hello world")
    assert str(r) == "hello world"
    assert "hello" in r


def test_agent_response_default_media_empty():
    r = AgentResponse()
    assert r.media_items == []


# ---------------------------------------------------------------------------
# Metadata header
# ---------------------------------------------------------------------------

def test_metadata_header_uses_utc_when_no_state():
    ts = datetime(2026, 4, 25, 14, 30, 0, tzinfo=timezone.utc)
    out = _inject_metadata_header("hi", ts, "telegram")
    assert "[Metadata: 2026-04-25 14:30:00 UTC | Platform: telegram]" in out
    assert out.endswith("\nhi")


def test_metadata_header_converts_to_user_timezone():
    ts = datetime(2026, 4, 25, 14, 30, 0, tzinfo=timezone.utc)
    out = _inject_metadata_header(
        "hi", ts, "cli",
        state={"user_preferences": "Timezone: America/New_York\nLanguage: en"},
    )
    assert "America" in out or "EDT" in out or "EST" in out


def test_metadata_header_handles_dict_preferences():
    ts = datetime(2026, 4, 25, 14, 30, 0, tzinfo=timezone.utc)
    out = _inject_metadata_header(
        "hi", ts, "cli", state={"user_preferences": {"timezone": "Asia/Tokyo"}}
    )
    # Tokyo is UTC+9 → 23:30 local
    assert "23:30" in out


def test_metadata_header_naive_timestamp_treated_as_utc():
    ts = datetime(2026, 4, 25, 14, 30, 0)  # naive
    out = _inject_metadata_header("hi", ts, "telegram")
    assert "14:30" in out
    assert "UTC" in out


# ---------------------------------------------------------------------------
# A2A agent card — multimodal + OAuth callback registration
# ---------------------------------------------------------------------------

def test_agent_card_includes_multimodal_input_modes():
    card = _build_agent_card()
    modes = set(card["defaultInputModes"])
    assert "text/plain" in modes
    assert "application/gzip" in modes  # DNA bundles
    assert "image/png" in modes
    assert "audio/mpeg" in modes
    assert "video/mp4" in modes


def test_agent_card_declares_dna_skill():
    card = _build_agent_card()
    skill_ids = {s["id"] for s in card["skills"]}
    assert "dna-exchange" in skill_ids
    assert "self-evolution" in skill_ids


def test_agent_card_includes_security_when_api_key_set(monkeypatch):
    monkeypatch.setenv("A2A_API_KEY", "test-key")
    card = _build_agent_card()
    assert "securitySchemes" in card
    assert card["securitySchemes"]["apiKey"]["name"] == "x-a2a-api-key"


def test_pending_oauth_registration_round_trip():
    register_pending_oauth("token-abc", "tg_42", "tg_chat_99")
    binding = _consume_pending_oauth("token-abc")
    assert binding == {"user_id": "tg_42", "session_id": "tg_chat_99"}
    # Single-use: second consume returns None.
    assert _consume_pending_oauth("token-abc") is None


def test_pending_oauth_unknown_token_returns_none():
    assert _consume_pending_oauth("never-registered") is None


# ---------------------------------------------------------------------------
# OAuth flow stitch — configure_integration → callback round-trip
# ---------------------------------------------------------------------------

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_configure_integration_registers_oauth_binding(monkeypatch):
    """configure_integration on an OAuth provider must register the state
    token with the pending-OAuth map BEFORE returning the authorize URL,
    so the callback can bind the resulting credential to the right user.
    """
    # Wipe any leftover bindings from other tests.
    from app.runtime.oauth_state import clear_all, list_pending_tokens
    clear_all()

    # Mock vault so the provider has client credentials.
    monkeypatch.setattr(
        "app.integrations.base.vault.get",
        lambda key, default="": "client-id" if "CLIENT_ID" in key else "",
    )

    # Build a tool_context with a session + user_id state.
    from app.tools.integrations import configure_integration
    state_mock = MagicMock()
    state_mock.to_dict.return_value = {"user_id": "tg_42"}
    sess_mock = MagicMock()
    sess_mock.session_id = "tg_chat_99"
    sess_mock.id = "tg_chat_99"
    tc = MagicMock()
    tc.state = state_mock
    tc.session = sess_mock

    result = await configure_integration("google", tool_context=tc)

    assert result["status"] == "awaiting_user"
    state_token = result["state"]
    assert state_token  # non-empty

    # The state token must now be in the pending map. Consume it and verify
    # the binding is correct.
    binding = _consume_pending_oauth(state_token)
    assert binding == {"user_id": "tg_42", "session_id": "tg_chat_99"}
    # Map is now empty.
    assert list_pending_tokens() == []
