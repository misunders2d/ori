from app.app_utils.config import AGENT_CONFIG_KEYS, ALLOWED_CONFIG_KEYS
from app.app_utils.models import PROVIDER_API_KEYS, SUPPORTED_PROVIDERS


def test_supported_provider_api_keys_are_configurable():
    """Every model provider API key must be accepted by config flows."""
    assert set(PROVIDER_API_KEYS) == set(SUPPORTED_PROVIDERS)

    missing_allowed = {
        provider: key
        for provider, key in PROVIDER_API_KEYS.items()
        if key not in ALLOWED_CONFIG_KEYS
    }
    assert missing_allowed == {}

    missing_agent_config = {
        provider: key
        for provider, key in PROVIDER_API_KEYS.items()
        if key not in AGENT_CONFIG_KEYS
    }
    assert missing_agent_config == {}
