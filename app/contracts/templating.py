"""Template rendering for contract args + emit templates.

Two surface use cases:

1. Loader args. Before a loader runs, every string value in ``args`` is
   scanned for ``{path.to.field}`` placeholders; each placeholder is
   replaced with the corresponding value from the fire state dict.
   Lists and nested dicts are walked recursively.

2. Reasoning ``user_template`` and emit ``args`` text. Same scan, same
   substitution.

Placeholder grammar — kept deliberately simple:

  - ``{name}``               → ``state["name"]``
  - ``{name.key}``           → ``state["name"]["key"]``
  - ``{name.key.subkey}``    → ``state["name"]["key"]["subkey"]``
  - ``{name[0]}``            → ``state["name"][0]``
  - ``{name[0].title}``      → ``state["name"][0]["title"]``

Unresolved placeholders raise ``TemplateError`` — the executor reports
this as an authoring bug (the dry-run should have caught it), aborts
the fire, and routes through ``on_failure``. Silent fallbacks here
would hide drift; the whole point of the contract path is to surface
mismatches early.

A handful of magic placeholders resolve from a small standard library
of fire-time values (``{today}``, ``{now}``, ``{today-5d}``, etc.) — see
``_special()``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any


class TemplateError(ValueError):
    """Raised when a placeholder can't be resolved against the fire
    state. Indicates a bug in the contract (or a missing loader output).
    The executor must abort the fire and not paper over the gap."""


_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")


def _resolve_path(path: str, state: dict[str, Any]) -> Any:
    """Walk ``state`` along ``path`` and return the leaf value.

    Supports dotted attrs (``a.b.c``) and integer subscripts
    (``a[0].b``). No arbitrary expressions — we don't want contracts
    smuggling Python in.
    """
    # Tokenise: split on '.' and '[' but keep the bracket bits.
    tokens: list[str] = []
    for chunk in path.split("."):
        # Each chunk may end in one or more "[idx]" subscripts.
        while "[" in chunk:
            head, _, rest = chunk.partition("[")
            if head:
                tokens.append(head)
            idx_str, _, chunk = rest.partition("]")
            if not idx_str:
                raise TemplateError(f"empty subscript in {path!r}")
            tokens.append(idx_str)  # subscript token kept as string
        if chunk:
            tokens.append(chunk)

    cur: Any = state
    for tok in tokens:
        if isinstance(cur, list):
            try:
                idx = int(tok)
            except ValueError as e:
                raise TemplateError(
                    f"path {path!r}: cannot index list with non-int {tok!r}"
                ) from e
            try:
                cur = cur[idx]
            except IndexError as e:
                raise TemplateError(
                    f"path {path!r}: list index {idx} out of range"
                ) from e
        elif isinstance(cur, dict):
            if tok not in cur:
                raise TemplateError(
                    f"path {path!r}: key {tok!r} not in state "
                    f"(have: {sorted(cur.keys())[:10]})"
                )
            cur = cur[tok]
        else:
            raise TemplateError(
                f"path {path!r}: cannot traverse {type(cur).__name__} with {tok!r}"
            )
    return cur


def _special(name: str) -> Any:
    """Resolve a magic placeholder, or raise ``KeyError`` if unknown.

    Currently supported:
      - ``today``        → ``YYYY-MM-DD`` (UTC)
      - ``now``          → ISO-8601 timestamp (UTC)
      - ``today-Nd``     → ``YYYY-MM-DD`` N days ago
      - ``today+Nd``     → ``YYYY-MM-DD`` N days forward
    """
    if name == "today":
        return date.today().isoformat()
    if name == "now":
        return datetime.now(timezone.utc).isoformat()
    m = re.fullmatch(r"today([+-])(\d+)d", name)
    if m:
        sign, n = m.group(1), int(m.group(2))
        delta = timedelta(days=n if sign == "+" else -n)
        return (date.today() + delta).isoformat()
    raise KeyError(name)


def _render_string(s: str, state: dict[str, Any]) -> str:
    """Substitute every ``{path}`` placeholder in ``s``.

    Special placeholders (``today``, ``now``, ``today-Nd``) resolve from
    the standard library; everything else resolves against the fire
    state dict.
    """

    def _sub(m: re.Match) -> str:
        path = m.group(1).strip()
        try:
            value = _special(path)
        except KeyError:
            value = _resolve_path(path, state)
        return _stringify(value)

    return _PLACEHOLDER_RE.sub(_sub, s)


def _stringify(value: Any) -> str:
    """Convert a resolved placeholder value to its template-friendly
    string form. Dicts/lists become compact JSON so prompts can feed
    them directly to an LLM as structured input."""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return "" if value is None else str(value)
    # Dict / list / nested: compact JSON.
    import json

    return json.dumps(value, ensure_ascii=False)


def render(value: Any, state: dict[str, Any]) -> Any:
    """Recursively render ``value`` against ``state``.

    Strings get placeholder substitution. Dicts / lists / tuples get
    walked and their members rendered. Anything else is returned as-is.
    """
    if isinstance(value, str):
        return _render_string(value, state)
    if isinstance(value, list):
        return [render(v, state) for v in value]
    if isinstance(value, tuple):
        return tuple(render(v, state) for v in value)
    if isinstance(value, dict):
        return {k: render(v, state) for k, v in value.items()}
    return value
