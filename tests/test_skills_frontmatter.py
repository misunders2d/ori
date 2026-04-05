import pathlib
import os

def test_skills_md_yaml_frontmatter():
    """Ensure all SKILL.md files start with --- (YAML frontmatter)."""
    # In sandbox verification, we are usually in data/sandbox/
    # The real skills directory is at the project root level.
    
    # Try multiple common relative paths to find the skills directory
    possible_paths = [
        pathlib.Path("skills"),                     # If running from project root
        pathlib.Path("../../skills"),               # If running from data/sandbox/
        pathlib.Path(__file__).parent.parent / "skills", # Relative to test file
        pathlib.Path(__file__).parent.parent.parent / "skills", # Relative to test file in sandbox
    ]
    
    skills_dir = None
    for p in possible_paths:
        if p.exists() and p.is_dir():
            skills_dir = p
            break
            
    assert skills_dir is not None, f"Skills directory not found. Checked: {[str(p.absolute()) for p in possible_paths]}. CWD: {os.getcwd()}"
    
    skill_files = list(skills_dir.glob("*/SKILL.md"))
    assert len(skill_files) > 0, f"No SKILL.md files found in {skills_dir.absolute()}"
    
    for skill_file in skill_files:
        with open(skill_file, "r", encoding="utf-8") as f:
            content = f.read().lstrip()
            # We check the first 3 characters explicitly for "---"
            assert content.startswith("---"), f"Skill {skill_file.relative_to(skills_dir.parent)} does not start with '---' (YAML frontmatter). Found start: {content[:10]!r}"
