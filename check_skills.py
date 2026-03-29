import pathlib
import os

skills_dir = pathlib.Path("/code/skills")
skill_files = list(skills_dir.glob("**/SKILL.md")) # Recursive

for skill_file in skill_files:
    with open(skill_file, "r", encoding="utf-8") as f:
        content = f.read().lstrip()
        if not content.startswith("---"):
            print(f"FAIL: {skill_file}")
        else:
            print(f"PASS: {skill_file}")
