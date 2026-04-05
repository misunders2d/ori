import inspect
import logging
from typing import get_type_hints, List, Any, Union, Optional

logger = logging.getLogger(__name__)

def validate_tool_schema(func: callable) -> None:
    """
    Validates that a function's signature is compatible with the Gemini API tool declaration.
    Specifically checks for common pitfalls like missing subtypes for list/array parameters.
    
    Raises:
        TypeError: If the function signature is likely to cause a 400 INVALID_ARGUMENT from Gemini.
    """
    try:
        sig = inspect.signature(func)
        hints = get_type_hints(func)
    except Exception as e:
        logger.warning(f"Could not introspect function {func.__name__}: {e}")
        return

    for param_name, param in sig.parameters.items():
        # Skip internal parameters
        if param_name in ["tool_context", "context"]:
            continue
            
        hint = hints.get(param_name)
        if hint is None:
            # Gemini might default to string, but it's better to have a hint
            continue
            
        # Check for plain list
        if hint is list:
            raise TypeError(
                f"Tool '{func.__name__}' parameter '{param_name}' is typed as plain 'list'. "
                f"Gemini requires a subtype (e.g. list[str]) to generate the mandatory 'items' field."
            )
            
        # Check for typing.List without args
        if hasattr(hint, "__origin__") and (hint.__origin__ is list or hint.__origin__ is List):
            if not hasattr(hint, "__args__") or not hint.__args__:
                 raise TypeError(
                    f"Tool '{func.__name__}' parameter '{param_name}' is missing a subtype for List. "
                    f"Gemini requires a subtype (e.g. List[str]) to generate the mandatory 'items' field."
                )

def validate_agent_tools(agent) -> None:
    """Validates all tools of an agent and its sub-agents recursively."""
    seen_agents = set()
    seen_tools = set()

    def _walk(a):
        if a in seen_agents:
            return
        seen_agents.add(a)
        
        for tool in a.tools:
            func = None
            if callable(tool):
                func = tool
            elif hasattr(tool, "function"):
                func = tool.function
            elif hasattr(tool, "_function"):
                func = tool._function
            
            if func and func not in seen_tools:
                seen_tools.add(func)
                validate_tool_schema(func)
        
        for sub in a.sub_agents:
            _walk(sub)

    _walk(agent)
