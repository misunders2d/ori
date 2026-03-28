import subprocess
import sys

def run():
    res = subprocess.run([sys.executable, "-m", "pytest", "tests/test_schema_validation.py"], capture_output=True, text=True)
    print(res.stdout)
    print(res.stderr)

if __name__ == "__main__":
    run()
