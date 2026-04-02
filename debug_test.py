import os
import importlib
import app.callbacks.guardrails

importlib.reload(app.callbacks.guardrails)

print(f"DEBUG: Guardrails file path: {app.callbacks.guardrails.__file__}")

with open(app.callbacks.guardrails.__file__, 'r') as f:
    content = f.read()
    print(f"DEBUG: Content starting with: {content[:100]}")
