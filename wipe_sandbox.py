import shutil
import os

def wipe():
    sandbox_dir = os.path.abspath("./data/sandbox")
    if os.path.exists(sandbox_dir):
        shutil.rmtree(sandbox_dir)
        print("Sandbox wiped.")
    else:
        print("Sandbox already empty.")

wipe()
