import inspect

try:
    from google.adk.tools.mcp_tool import McpToolset
    print(f"McpToolset init signature: {inspect.signature(McpToolset.__init__)}")
except ImportError as e:
    print(f"Import failed: {e}")
except Exception as e:
    print(f"Error: {e}")
