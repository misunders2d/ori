import inspect
from typing import get_type_hints, List, Any
from app.sub_agents.coordinator_agent import root_agent

def validate_tool_schema(func: callable) -> None:
    try:
        sig = inspect.signature(func)
        hints = get_type_hints(func)
    except Exception as e:
        print(f"Error getting hints for {func.__name__}: {e}")
        return

    for param_name, param in sig.parameters.items():
        if param_name in ["tool_context", "context"]:
            continue
            
        hint = hints.get(param_name)
        if hint is list:
             print(f"FAIL: {func.__name__} - {param_name} is plain list")
        if hasattr(hint, "__origin__") and (hint.__origin__ is list or hint.__origin__ is List):
            if not hasattr(hint, "__args__") or not hint.__args__:
                 print(f"FAIL: {func.__name__} - {param_name} is missing subtype for List")

seen_agents = set()
seen_tools = set()

def _walk(a):
    if id(a) in seen_agents:
        return
    seen_agents.add(id(a))
    
    for tool in a.tools:
        func = None
        if callable(tool):
            func = tool
        elif hasattr(tool, "function"):
            func = tool.function
        elif hasattr(tool, "_function"):
            func = tool._function
        
        if func and id(func) not in seen_tools:
            seen_tools.add(id(func))
            validate_tool_schema(func)
    
    for sub in getattr(a, "sub_agents", []):
        _walk(sub)

_walk(root_agent)
