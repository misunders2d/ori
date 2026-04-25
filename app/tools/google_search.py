"""Google Search agent tool — gracefully unavailable when no Google credentials."""

import logging
import os

logger = logging.getLogger(__name__)


def _has_google_credentials() -> bool:
    """Check if Google API credentials are available (API key or Vertex AI)."""
    if os.environ.get("GOOGLE_API_KEY", "").strip():
        return True
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE":
        return True
    return False


google_search_agent_tool = None

if _has_google_credentials():
    try:
        from google.adk.agents import Agent
        from google.adk.tools.google_search_agent_tool import GoogleSearchAgentTool
        from google.adk.tools.google_search_tool import google_search

        from app.util.models import get_model

        # Match ADK's reference factory `create_google_search_agent` exactly —
        # a single-purpose sub-agent whose only tool is google_search. We do
        # NOT set `include_server_side_tool_invocations=True`: that flag is
        # for "tool context circulation" (Gemini 3+), needed only when
        # combining google_search with other tools in the same request.
        # Our sub-agent has only google_search, so the flag is unnecessary
        # and forces a 400 "Tool call context circulation is not enabled"
        # on any pre-Gemini-3 model.
        google_search_agent_tool = GoogleSearchAgentTool(
            agent=Agent(
                name="google_search_agent",
                model=get_model("google_search"),
                description="An agent that performs web search using google search tool",
                tools=[google_search],
            )
        )
    except Exception as e:
        logger.warning("Google Search agent unavailable: %s", e)
        google_search_agent_tool = None
else:
    logger.info("Google Search agent disabled — no Google credentials configured.")
