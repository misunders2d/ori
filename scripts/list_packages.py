import subprocess
import sys

def list_packages():
    result = subprocess.run([sys.executable, "-m", "pip", "list"], capture_output=True, text=True)
    print(result.stdout)

if __name__ == "__main__":
    list_packages()
