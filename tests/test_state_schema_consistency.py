"""Static guard: every key the codebase writes to session state must be
declared in OriSessionState.

ADK 2.0's State validator rejects writes to undeclared keys at runtime —
even with `extra="allow"` on the Pydantic model (that flag only affects
model_validate, not the State setitem path). A missing declaration crashes
the agent on the first turn that exercises the offending code path.

This test scans `app/` for every `state['key'] = ...` write and asserts
each key is either declared on OriSessionState or in the explicit allowlist
below (transient keys ADK manages internally).
"""

import re
from pathlib import Path

from app.state import OriSessionState

# Keys ADK manages internally on the State object — not part of OriSessionState
# but written by ADK or its plugins, not by our code.
ADK_INTERNAL_KEYS: set[str] = set()


def _find_state_writes() -> dict[str, list[str]]:
    """Scan app/ for state['key'] = ... patterns. Returns {key: [files]}."""
    project_root = Path(__file__).resolve().parents[1]
    app_dir = project_root / "app"
    qualified = re.compile(
        r'(?:ctx|tool_context|callback_context|sess|session)\.state\['
        r'["\']([a-zA-Z_][a-zA-Z0-9_]*)["\']'
        r'\]\s*='
    )
    bare = re.compile(
        r'(?<![\.\w])state\['
        r'["\']([a-zA-Z_][a-zA-Z0-9_]*)["\']'
        r'\]\s*='
    )
    found: dict[str, list[str]] = {}
    for path in app_dir.rglob("*.py"):
        try:
            src = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(project_root))
        for m in qualified.finditer(src):
            found.setdefault(m.group(1), []).append(rel)
        for m in bare.finditer(src):
            found.setdefault(m.group(1), []).append(rel)
    return found


def test_all_state_writes_declared_in_schema():
    """Every state['key'] = ... must use a key declared in OriSessionState
    (or be in the ADK-internal allowlist).
    """
    declared = set(OriSessionState.model_fields) | ADK_INTERNAL_KEYS
    writes = _find_state_writes()
    undeclared = {k: sorted(set(v)) for k, v in writes.items() if k not in declared}
    assert not undeclared, (
        "State writes use keys not declared in OriSessionState. "
        "Either add them to app/state.py:OriSessionState or to "
        "ADK_INTERNAL_KEYS in this test:\n"
        + "\n".join(f"  {k!r:30} written in {files}" for k, files in undeclared.items())
    )
