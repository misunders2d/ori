# Model Context Protocol (MCP) Implementation Guide

MCP provides a standard connection pattern for hundreds of servers. Servers advertise tools, and your agent discovers them automatically.

## Standard ADK Pattern

```python
from google.adk.agents import Agent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

# Example: Notion MCP
notion_tools = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="npx", 
            args=["-y", "@notionhq/notion-mcp-server"],
            env={"NOTION_TOKEN": NOTION_TOKEN}
        ),
        timeout=30
    )
)

agent = Agent(
    tools=[notion_tools],
    # ... other config
)
```

## Available MCP Toolbox (First-Party)
ADK provides `ToolboxToolset` for standard Google and database integrations:

- **Google Cloud API Registry**: Connect to GCP services as MCP tools.
- **MCP Toolbox for Databases**: PostgreSQL, SQLite, BigQuery, etc.
- **Code Execution**: Run AI-generated code via GKE Agent Engine.

## Common Third-Party Servers
| Server | npx package | Req. Env Var |
|--------|-------------|--------------|
| **Notion** | `@notionhq/notion-mcp-server` | `NOTION_TOKEN` |
| **Mailgun** | `@mailgun/mcp-server` | `MAILGUN_API_KEY` |
| **GitHub** | `@modelcontextprotocol/server-github` | `GITHUB_PERSONAL_ACCESS_TOKEN` |
| **Slack** | `@modelcontextprotocol/server-slack` | `SLACK_BOT_TOKEN` |

## Discovery Flow
1. **Identify**: Find the server at `https://google.github.io/adk-docs/integrations/?topic=mcp`.
2. **Configure**: Use `configure_integration` to prompt the user for the required API key.
3. **Mount**: Add the `McpToolset` to the appropriate agent (Coordinator or a specialized sub-agent).
