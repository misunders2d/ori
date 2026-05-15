"""V2 scheduler — authoring owner-id env fallback.

Phase 9 slice 6 per ``docs/PHASE_9_PLAN.md`` §3.6 + §5.5 +
§6 (cross-cutting smoke checks).

Phase-7 round-2 reviewer finding L365 / Q10 deferred the
``expected_owner_id`` env-fallback story: the
:class:`~app.v2.toolsets.authoring.AuthoringToolset`
constructor accepts an explicit kwarg (production wiring
threads it through from the agent layer), but in
``run_bot.py`` boot we want the same fallback the v1 scheduler
uses today — read the deployed owner id from the
``V2_AUTHORING_OWNER_ID`` environment variable so a single
container can carry the tenant id without code changes.

The module exposes a single module-level constant
:data:`DEFAULT_AUTHORING_OWNER_ID` read **ONCE at import
time** from ``os.environ``. Empty / whitespace-only env
values are normalised to ``None`` so a missing var and a
``V2_AUTHORING_OWNER_ID=""`` deploy mistake surface the
same way (constructor raises with the same clear message
instead of silently registering ``""`` as a tenant id).

Reading env at import time matches the plan literal
wording (§3.6) and the phase-5 hard rule 10 documented
exception. ``AuthoringToolset.__init__`` consumes the
constant by way of ``_owner_default.DEFAULT_AUTHORING_OWNER_ID``
attribute access on the module object — so test code can
monkeypatch the constant to drive the explicit-kwarg-wins /
env-fallback / both-None branches without re-importing the
module.

Pin: this is the documented exception to phase-5 hard rule
10 (``_defaults.py`` is the sole production wiring for the
v2 runtime clock + id factories). The import-hygiene smoke
checks (``tests/v2/test_runtime_owner_default.py``)
allowlist this single module for ``os.environ`` reads at
module load; every other ``app.v2.runtime.*`` module is
asserted to NOT read ``os.environ`` at module load.

References:
- ``docs/PHASE_9_PLAN.md`` §3.6 (this module's contract).
- ``docs/PHASE_9_PLAN.md`` §5.5 (test pins).
- ``docs/PHASE_9_PLAN.md`` §6 (cross-cutting smoke checks /
  documented exception).
- ``docs/PHASE_7_PLAN.md`` round-2 reviewer L365 / Q10
  (the deferred kwarg-default story this module closes).
"""

from __future__ import annotations

import os
from typing import Optional


_RAW_ENV = os.environ.get("V2_AUTHORING_OWNER_ID")


DEFAULT_AUTHORING_OWNER_ID: Optional[str] = (
    _RAW_ENV.strip() if (_RAW_ENV is not None and _RAW_ENV.strip()) else None
)
"""Module-level constant captured ONCE at import time.

``None`` when ``V2_AUTHORING_OWNER_ID`` is unset, empty, or
whitespace-only. Otherwise the stripped string value.
"""


__all__ = ["DEFAULT_AUTHORING_OWNER_ID"]
