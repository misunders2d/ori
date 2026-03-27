from google.adk.agents import Agent
from google.adk.tools.google_search_agent_tool import GoogleSearchAgentTool
from google.adk.tools.google_search_tool import google_search
from google.adk.models import Gemini
from google.genai import types

google_search_agent_tool = GoogleSearchAgentTool(
    agent=Agent(
        name="google_search_agent",
        model=Gemini(model="gemini-3-flash-preview"),
        description="An agent that performs web search using google search tool",
        tools=[google_search],
        generate_content_config=types.GenerateContentConfig(
            tool_config=types.ToolConfig(include_server_side_tool_invocations=True)
        ),
    )
)
