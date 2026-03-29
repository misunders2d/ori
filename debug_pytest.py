import subprocess
import sys

result = subprocess.run([sys.executable, "-m", "pytest", "-v", "/code/tests/test_skills_frontmatter.py"], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)
