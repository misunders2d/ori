import os
import subprocess

print("Listing all files in /code (including hidden):")
result = subprocess.run(["ls", "-la", "/code"], capture_output=True, text=True)
print(result.stdout)
