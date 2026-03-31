import os
import json
from app.app_utils.config import ALLOWED_CONFIG_KEYS
from dotenv import load_dotenv
load_dotenv('data/.env')

from app.tools.a2a import export_dna
from app.callbacks.guardrails import a2a_privacy_guardrail
from unittest.mock import MagicMock

tool = MagicMock()
tool.name = "export_dna"

resp = export_dna(None)

res = a2a_privacy_guardrail(tool, {}, None, tool_response=resp)
print("GUARDRAIL RESULT:", res)
