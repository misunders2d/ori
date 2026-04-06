"""Data visualization tool — executes plotting code in a restricted scope.

The agent writes Python plotting code, this tool executes it with only
plotting libraries available, and saves the output to tmp/plots/.
Returns the file path for the transport layer to deliver.
"""

import logging
import os
import time
import traceback
import uuid

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_PLOTS_DIR = os.path.abspath("./tmp/plots")

# Restricted set of allowed imports for the plotting sandbox
_ALLOWED_MODULES = {
    "matplotlib": __import__("matplotlib"),
    "matplotlib.pyplot": __import__("matplotlib.pyplot", fromlist=["pyplot"]),
    "matplotlib.dates": __import__("matplotlib.dates", fromlist=["dates"]),
    "matplotlib.ticker": __import__("matplotlib.ticker", fromlist=["ticker"]),
    "matplotlib.colors": __import__("matplotlib.colors", fromlist=["colors"]),
    "numpy": __import__("numpy"),
    "json": __import__("json"),
    "math": __import__("math"),
    "datetime": __import__("datetime"),
}

# Lazy imports — only loaded if the agent uses them
_LAZY_MODULES = {"plotly", "plotly.graph_objects", "plotly.express", "plotly.io", "seaborn", "pandas"}


def _get_module(name: str):
    """Get a module from allowed set, lazy-loading plotly/seaborn/pandas on demand."""
    if name in _ALLOWED_MODULES:
        return _ALLOWED_MODULES[name]
    if name in _LAZY_MODULES:
        try:
            parts = name.split(".")
            mod = __import__(name, fromlist=[parts[-1]] if len(parts) > 1 else [])
            _ALLOWED_MODULES[name] = mod
            return mod
        except ImportError:
            return None
    return None


def _restricted_import(name, *args, **kwargs):
    """Custom __import__ that only allows plotting-related modules."""
    mod = _get_module(name)
    if mod is not None:
        return mod
    raise ImportError(f"Import '{name}' is not allowed in the visualization sandbox. "
                      f"Allowed: matplotlib, plotly, seaborn, pandas, numpy, json, math, datetime.")


# Force matplotlib to non-interactive backend
import matplotlib
matplotlib.use("Agg")


def generate_chart(
    code: str,
    filename: str = "",
    tool_context: ToolContext = None,
) -> dict:
    """Execute Python plotting code and save the output as an image or HTML file.

    Write standard matplotlib, plotly, or seaborn code. The output file is saved
    automatically — use `plt.savefig(OUTPUT_PATH)` for matplotlib or
    `fig.write_html(OUTPUT_PATH)` / `fig.write_image(OUTPUT_PATH)` for plotly.

    The variable OUTPUT_PATH is pre-set to the correct file path.

    Args:
        code: Python code that generates a chart. Has access to: matplotlib,
              plotly, seaborn, pandas, numpy, json, math, datetime.
              Use OUTPUT_PATH as the save destination.
        filename: Optional filename (e.g., 'sales_chart.png'). Auto-generated if empty.
                  Use .png for images, .html for interactive plotly charts.

    Returns:
        dict: Status and file path of the generated chart.
    """
    if not code or not code.strip():
        return {"status": "error", "message": "No code provided. Write Python plotting code using matplotlib, plotly, or seaborn."}

    os.makedirs(_PLOTS_DIR, exist_ok=True)

    # Determine output path
    if not filename:
        filename = f"chart_{uuid.uuid4().hex[:8]}.png"
    output_path = os.path.join(_PLOTS_DIR, filename)

    # Build restricted execution scope
    scope = {
        "__builtins__": {
            "print": print,
            "len": len,
            "range": range,
            "enumerate": enumerate,
            "zip": zip,
            "sorted": sorted,
            "min": min,
            "max": max,
            "sum": sum,
            "abs": abs,
            "round": round,
            "int": int,
            "float": float,
            "str": str,
            "list": list,
            "dict": dict,
            "tuple": tuple,
            "set": set,
            "bool": bool,
            "True": True,
            "False": False,
            "None": None,
            "__import__": _restricted_import,
            "isinstance": isinstance,
            "hasattr": hasattr,
            "getattr": getattr,
        },
        "OUTPUT_PATH": output_path,
    }

    # Pre-load commonly used modules into scope
    scope["plt"] = _get_module("matplotlib.pyplot")
    scope["np"] = _get_module("numpy")
    scope["matplotlib"] = _get_module("matplotlib")
    scope["datetime"] = _get_module("datetime")
    scope["json"] = _get_module("json")
    scope["math"] = _get_module("math")

    try:
        exec(code, scope)

        # Auto-save if matplotlib figure exists and file wasn't saved by code
        if not os.path.exists(output_path):
            plt = _get_module("matplotlib.pyplot")
            if plt and plt.get_fignums():
                plt.tight_layout()
                plt.savefig(output_path, dpi=150, bbox_inches="tight")
                plt.close("all")

        if os.path.exists(output_path):
            size = os.path.getsize(output_path)
            logger.info("Chart saved: %s (%d bytes)", output_path, size)
            return {
                "status": "success",
                "file_path": output_path,
                "filename": filename,
                "size_bytes": size,
            }
        else:
            return {
                "status": "error",
                "message": "Code executed but no file was saved. Make sure to use plt.savefig(OUTPUT_PATH) or fig.write_html(OUTPUT_PATH).",
            }

    except ImportError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Chart generation failed:\n%s", tb)
        return {
            "status": "error",
            "message": f"Chart code failed: {e}",
            "traceback": tb.split("\n")[-3:],
        }
    finally:
        # Always clean up matplotlib state
        try:
            plt = _get_module("matplotlib.pyplot")
            if plt:
                plt.close("all")
        except Exception:
            pass
