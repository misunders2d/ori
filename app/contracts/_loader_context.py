"""Minimal ToolContext stand-in for loader use.

Loaders run outside of an ADK agent session. Some existing tool
functions (``web_fetch``, ``search_knowledge``) accept a ``ToolContext``
parameter and read at most ``state`` + ``session_id`` from it. This
shim provides just enough surface to keep those calls happy.

If a tool *requires* a real ToolContext (e.g. for delegated auth that
needs the runner's user_id), the contract loader should call the
underlying core helper directly instead of routing through this shim
— see ``graph_query`` for the pattern (uses ``app.core.graph._get_driver``,
not the agent-facing tool).
"""

from __future__ import annotations

from typing import Any


class _ShimState:
    """Mimics ``ToolContext.state`` enough for read-only access from
    loader-invoked tools. Reads return defaults; writes are silently
    discarded (loaders are pure data fetchers — they have no business
    mutating session state)."""

    def __init__(self):
        self._d: dict[str, Any] = {
            "user_id": "contract_loader",
            "session_id": "contract_loader",
            "docs_read": {
                "docs/AI_EDITS.md": True,
                "docs/INDEX.md": True,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return dict(self._d)

    def __setitem__(self, k: str, v: Any) -> None:
        # Loader-side writes are dropped — see class docstring.
        pass

    def __getitem__(self, k: str) -> Any:
        return self._d[k]

    def get(self, k: str, default=None) -> Any:
        return self._d.get(k, default)


class _ShimSession:
    session_id = "contract_loader"


class LoaderContext:
    """Looks enough like ``ToolContext`` for the limited surface loaders
    actually exercise (``state.to_dict()``, ``session.session_id``).
    Real fields beyond these raise AttributeError — surfacing a wrong
    call early during dry-run rather than silently doing the wrong
    thing in production."""

    def __init__(self):
        self.state = _ShimState()
        self.session = _ShimSession()
