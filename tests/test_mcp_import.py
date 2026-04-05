
try:
    from google.adk.tools.mcp_tool import McpToolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    print("google.adk.tools.mcp_tool imports successful")
except ImportError as e:
    print(f"google.adk.tools.mcp_tool imports failed: {e}")

try:
    from mcp import StdioServerParameters
    print("mcp imports successful")
except ImportError as e:
    print(f"mcp imports failed: {e}")
