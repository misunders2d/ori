import sys
import os

print(f"PYTHONPATH: {sys.path}")
print(f"CWD: {os.getcwd()}")

try:
    import app.app_utils
    print(f"app.app_utils path: {app.app_utils.__path__}")
    import app.app_utils.schema_validator
    print("Imported schema_validator successfully!")
except Exception as e:
    import traceback
    traceback.print_exc()
