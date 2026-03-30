import json
import os

from a2a.types import AgentCard


AGENT_JSON = os.path.join(os.path.dirname(__file__), os.pardir, "agent.json")


def test_agent_card_validates():
    with open(AGENT_JSON) as f:
        card = AgentCard(**json.load(f))

    assert card.name
    assert card.url
    assert card.skills
    for skill in card.skills:
        assert skill.id
        assert skill.tags


def test_build_agent_card_validates():
    """The dynamically built card must also pass AgentCard validation."""
    from app.a2a_server import _build_agent_card

    card = AgentCard(**_build_agent_card())

    assert card.name
    assert card.url
    assert card.default_input_modes
    assert card.default_output_modes
    assert card.provider.organization
    for skill in card.skills:
        assert skill.id
        assert skill.tags
