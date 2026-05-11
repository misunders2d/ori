"""PresentationToolset — PowerPoint deck generation.

Mounts ``generate_presentation`` on the agent that owns data-analysis +
visualization work (``AmazonDataAnalystAgent``). The output ``.pptx``
flows through the file-attachment plumbing in ``app/callbacks/guardrails.py``
so it lands as a Slack / Telegram / A2A attachment automatically — no
caller-side wiring required.
"""

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class PresentationToolset(BaseToolset):
    """Build PowerPoint decks for ads, sales, warehouse, inventory,
    ASIN-audit, or any other business report. Uses
    ``data/presentations/templates/default.pptx`` (or a named template)
    when present for brand consistency; otherwise falls back to a blank
    deck with the python-pptx default theme.
    """

    async def get_tools(self, readonly_context=None):
        from app.tools.presentations import generate_presentation

        return [FunctionTool(func=generate_presentation)]
