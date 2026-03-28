import inspect
import pytest
from typing import get_type_hints, List, Any
from app.sub_agents.coordinator_agent import root_agent

def validate_tool_schema(func: callable) -> None:
    """Checks for common pitfalls in Gemini tool declarations."""
    try:
        sig = inspect.signature(func)
        hints = get_type_hints(func)
    except Exception:
        return

    for param_name, param in sig.parameters.items():
        if param_name in ["tool_context", "context"]:
            continue
            
        hint = hints.get(param_name)
        if hint is list:
             raise TypeError(
                f"Tool '{func.__name__}' parameter '{param_name}' is typed as plain 'list'. "
                f"Gemini requires a subtype (e.g. list[str]) to generate the mandatory 'items' field."
            )
        if hasattr(hint, "__origin__") and (hint.__origin__ is list or hint.__origin__ is List):
            if not hasattr(hint, "__args__") or not hint.__args__:
                 raise TypeError(
                    f"Tool '{func.__name__}' parameter '{param_name}' is missing a subtype for List. "
                    f"Gemini requires a subtype (e.g. List[str]) to generate the mandatory 'items' field."
                )

def test_all_agent_tools_gemini_schema_validity():
    """
    REGRESSION TEST: Ensures all agent tools produce valid schemas for the Gemini API.
    Specifically checks for the 'items' field requirement in 'array' parameters.
    """
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

    try:
        _walk(root_agent)
    except TypeError as e:
        pytest.fail(f"Gemini Tool Schema Validation Failed: {e}")
