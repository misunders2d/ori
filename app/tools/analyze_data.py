"""Data analysis tool — run pandas code on uploaded files.

Same sandbox pattern as export_file.py and visualize.py. The agent writes
Python code to analyze data, the tool executes it and returns the output.
"""

import logging
import os
import traceback

from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)

_UPLOADS_DIR = os.path.abspath("./tmp/uploads")

_ALLOWED_MODULES = {
    "json": __import__("json"),
    "csv": __import__("csv"),
    "math": __import__("math"),
    "datetime": __import__("datetime"),
    "io": __import__("io"),
    "os.path": __import__("os.path"),
}

_LAZY_MODULES = {"pandas", "openpyxl", "numpy", "collections", "re", "statistics"}


def _get_module(name: str):
    if name in _ALLOWED_MODULES:
        return _ALLOWED_MODULES[name]
    if name in _LAZY_MODULES:
        try:
            mod = __import__(name, fromlist=["_"])
            _ALLOWED_MODULES[name] = mod
            return mod
        except ImportError:
            return None
    return None


def _restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
    mod = _get_module(name)
    if mod is None:
        raise ImportError(f"Import '{name}' is not allowed. "
                          f"Allowed: pandas, openpyxl, numpy, json, csv, datetime, collections, re, statistics.")
    if fromlist:
        return mod
    parts = name.split(".")
    if len(parts) > 1:
        top = _get_module(parts[0])
        if top is not None:
            for i in range(1, len(parts)):
                subname = ".".join(parts[:i + 1])
                submod = _get_module(subname)
                if submod is not None:
                    setattr(_get_module(".".join(parts[:i])), parts[i], submod)
            return top
    return mod


def analyze_data(
    file_path: str,
    code: str,
    tool_context: ToolContext = None,
) -> dict:
    """Run Python/pandas analysis code on a data file and return the printed output.

    Use this tool to analyze uploaded spreadsheets and CSV files. The file is
    pre-loaded as a pandas DataFrame in the variable `df`. For Excel files with
    multiple sheets, `sheets` is a dict of {sheet_name: DataFrame}.

    Common operations:
    - Shape & columns: `print(df.shape); print(df.columns.tolist())`
    - Preview: `print(df.head(10))`
    - Statistics: `print(df.describe())`
    - Value counts: `print(df['column'].value_counts())`
    - Filtering: `print(df[df['column'] > 100])`
    - Groupby: `print(df.groupby('category')['sales'].sum())`
    - Missing values: `print(df.isnull().sum())`

    Args:
        file_path: Path to the data file (provided when the file was uploaded).
        code: Python code to analyze the data. The DataFrame is available as `df`.
              For multi-sheet Excel, use `sheets` dict. Print results to see them.
    """
    if not file_path:
        return {"status": "error", "message": "file_path is required. Use the path shown when the file was uploaded."}
    if not code or not code.strip():
        return {"status": "error", "message": "No analysis code provided."}

    # Security: only allow access to uploads dir
    abs_path = os.path.abspath(file_path)
    if not abs_path.startswith(_UPLOADS_DIR):
        return {"status": "error", "message": f"Access denied. Only files in {_UPLOADS_DIR} can be analyzed."}
    if not os.path.isfile(abs_path):
        return {"status": "error", "message": f"File not found: {file_path}"}

    import io as _io
    output_buf = _io.StringIO()

    scope = {
        "__builtins__": {
            "print": lambda *args, **kwargs: _custom_print(output_buf, *args, **kwargs),
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
            "setattr": setattr,
            "map": map,
            "filter": filter,
            "any": any,
            "all": all,
            "reversed": reversed,
            "type": type,
            "ValueError": ValueError,
            "TypeError": TypeError,
            "KeyError": KeyError,
            "IndexError": IndexError,
            "RuntimeError": RuntimeError,
            "Exception": Exception,
            "StopIteration": StopIteration,
            "AttributeError": AttributeError,
        },
    }

    # Pre-load common modules
    scope["json"] = _get_module("json")
    scope["csv"] = _get_module("csv")
    scope["datetime"] = _get_module("datetime")
    scope["math"] = _get_module("math")

    try:
        pd = _get_module("pandas")
        if pd is None:
            return {"status": "error", "message": "pandas is not installed."}
        scope["pd"] = pd

        # Load the data file
        ext = os.path.splitext(abs_path)[1].lower()
        if ext in (".xlsx", ".xls"):
            sheets = pd.read_excel(abs_path, sheet_name=None, engine="openpyxl")
            scope["sheets"] = sheets
            # Default df = first sheet
            first_sheet = next(iter(sheets.values()))
            scope["df"] = first_sheet
        elif ext == ".csv":
            scope["df"] = pd.read_csv(abs_path)
            scope["sheets"] = None
        else:
            return {"status": "error", "message": f"Unsupported file type: {ext}. Supported: .xlsx, .xls, .csv"}

        exec(code, scope)

        output = output_buf.getvalue()
        if not output.strip():
            output = "(no output — make sure to use print() to display results)"

        # Truncate very long output
        max_len = 15000
        if len(output) > max_len:
            output = output[:max_len] + f"\n... (output truncated at {max_len} chars)"

        return {"status": "success", "output": output}

    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Data analysis failed:\n%s", tb)
        return {
            "status": "error",
            "message": f"Analysis code failed: {e}",
            "traceback": tb.split("\n")[-3:],
        }


def _custom_print(buf, *args, **kwargs):
    """Redirect print() to a buffer."""
    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    buf.write(sep.join(str(a) for a in args) + end)
