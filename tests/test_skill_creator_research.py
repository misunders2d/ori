import os
import pytest

def test_skill_creator_contains_research_step():
    skill_path = "skills/skill-creator-skill/SKILL.md"
    if not os.path.exists(skill_path):
        pytest.skip("Skills directory not available in sandbox")

    with open(skill_path, "r") as f:
        content = f.read()

    # Check that research is a mandatory step before drafting
    assert "Research" in content, "Research step missing from SKILL.md"
    assert "google_search_agent_tool" in content, "Reference to google_search_agent_tool missing from SKILL.md"
    assert "web_fetch" in content, "Reference to web_fetch missing from SKILL.md"
    assert "NEVER draft without" in content or "NEVER proceed" in content, "Drafting prohibition without research missing from SKILL.md"
