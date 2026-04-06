"""File export tool — generates CSV, Excel, Markdown, PDF, or any file from code.

Same sandbox pattern as visualize.py. The agent writes Python code,
the tool executes it with data-processing libraries available.
"""

import logging
import os
import traceback
import uuid

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_EXPORTS_DIR = os.path.abspath("./tmp/exports")

# Lazy-loaded modules for the sandbox
_ALLOWED_MODULES = {
    "json": __import__("json"),
    "csv": __import__("csv"),
    "math": __import__("math"),
    "datetime": __import__("datetime"),
    "io": __import__("io"),
}

_LAZY_MODULES = {"pandas", "openpyxl", "reportlab", "reportlab.lib", "reportlab.lib.pagesizes",
                 "reportlab.lib.styles", "reportlab.lib.units", "reportlab.lib.colors",
                 "reportlab.platypus", "reportlab.pdfgen", "reportlab.pdfgen.canvas",
                 "numpy", "textwrap", "collections"}


def _get_module(name: str):
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
    mod = _get_module(name)
    if mod is not None:
        return mod
    raise ImportError(f"Import '{name}' is not allowed. "
                      f"Allowed: pandas, openpyxl, reportlab, numpy, json, csv, datetime, io, textwrap, collections.")


def generate_file(
    code: str,
    filename: str,
    tool_context: ToolContext = None,
) -> dict:
    """Execute Python code to generate an export file (CSV, Excel, Markdown, PDF, etc.).

    Write standard Python code to create the file. The variable OUTPUT_PATH is
    pre-set to the correct save location.

    Common patterns:
    - CSV: `pd.DataFrame(data).to_csv(OUTPUT_PATH, index=False)`
    - Excel: `pd.DataFrame(data).to_excel(OUTPUT_PATH, index=False)`
    - Markdown: `open(OUTPUT_PATH, 'w').write(md_text)`
    - PDF: Use reportlab (`from reportlab.pdfgen.canvas import Canvas`)

    Args:
        code: Python code that generates the file. Has access to: pandas,
              openpyxl, reportlab, numpy, json, csv, datetime, io.
              Use OUTPUT_PATH as the save destination.
        filename: Output filename with extension (e.g., 'report.csv', 'data.xlsx',
                  'summary.md', 'report.pdf'). Extension determines the file type.

    Returns:
        dict: Status and file path for delivery to the user.
    """
    os.makedirs(_EXPORTS_DIR, exist_ok=True)

    if not filename:
        filename = f"export_{uuid.uuid4().hex[:8]}.csv"
    output_path = os.path.join(_EXPORTS_DIR, filename)

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
            "open": open,
        },
        "OUTPUT_PATH": output_path,
    }

    # Pre-load common modules
    scope["json"] = _get_module("json")
    scope["csv"] = _get_module("csv")
    scope["datetime"] = _get_module("datetime")
    scope["math"] = _get_module("math")
    scope["io"] = _get_module("io")

    try:
        exec(code, scope)

        if os.path.exists(output_path):
            size = os.path.getsize(output_path)
            logger.info("File exported: %s (%d bytes)", output_path, size)
            return {
                "status": "success",
                "file_path": output_path,
                "filename": filename,
                "size_bytes": size,
            }
        else:
            return {
                "status": "error",
                "message": "Code executed but no file was saved. Write to OUTPUT_PATH.",
            }

    except ImportError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("File export failed:\n%s", tb)
        return {
            "status": "error",
            "message": f"Export code failed: {e}",
            "traceback": tb.split("\n")[-3:],
        }
