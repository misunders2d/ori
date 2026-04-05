import subprocess
import sys

def check_pip():
    result = subprocess.run([sys.executable, "-m", "pip", "list"], capture_output=True, text=True)
    print(result.stdout)

if __name__ == "__main__":
    check_pip()
