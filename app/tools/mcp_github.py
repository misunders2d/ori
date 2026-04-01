import os
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

# Initialize the GitHub MCP Toolset
# This toolset wraps the GitHub MCP server using npx.
# It requires GITHUB_TOKEN to be set in the environment.
github_mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx",
            args=["-y", "@modelcontextprotocol/server-github"],
            env={
                "GITHUB_PERSONAL_ACCESS_TOKEN": os.environ.get("GITHUB_TOKEN", "")
            },
        ),
        timeout=60,  # GitHub API can be slow sometimes
    )
)
