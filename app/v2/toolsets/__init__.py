"""V2 scheduler — agent-facing toolset bundles.

Phase 7 slice 6 ships the first v2 toolset bundle —
:class:`AuthoringToolset` — wrapping every authoring +
lifecycle tool as ADK ``FunctionTool`` instances. The
toolset is NOT mounted on any agent in phase 7; phase 9
cutover does the binding alongside the v1 → v2 flip.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.1
- ``docs/PHASE_7_PLAN.md`` §3.7
"""

from app.v2.toolsets.authoring import (
    AUTHORING_TOOL_DESCRIPTORS,
    AuthoringToolset,
)


__all__ = ["AUTHORING_TOOL_DESCRIPTORS", "AuthoringToolset"]
