import subprocess
import sys

def check_pkg(name):
    try:
        result = subprocess.run([sys.executable, "-m", "pip", "show", name], capture_output=True, text=True)
        print(result.stdout)
        if result.stderr:
            print("Error:", result.stderr)
    except Exception as e:
        print("Failed:", e)

check_pkg("google-adk")
