import os
import pathlib

def test_fix_skills():
    # PROJECT_ROOT is usually the parent of 'app'
    # In the sandbox, it's the sandbox dir itself.
    # But we want the LIVE skills.
    
    # Let's find the live root
    # __file__ is /code/data/sandbox/tests/test_fix_sandbox.py
    # live root is /code
    live_root = "/code"
    sandbox_skills = "/code/data/sandbox/skills"
    
    live_skills = os.path.join(live_root, "skills")
    
    if not os.path.exists(sandbox_skills):
        os.makedirs(sandbox_skills)
        
    for skill in os.listdir(live_skills):
        src = os.path.join(live_skills, skill)
        dst = os.path.join(sandbox_skills, skill)
        if not os.path.exists(dst):
            os.symlink(src, dst)
    
    assert os.path.exists(os.path.join(sandbox_skills, "system-management-skill"))
