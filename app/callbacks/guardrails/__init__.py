"""Guardrail callback package.

Was a single 1255-LOC `guardrails.py` until 2026-05-12. Split into
per-concern modules; this `__init__.py` re-exports every public callback
so existing imports (`from app.callbacks.guardrails import X`) keep
working across all ~20 callers without any rewiring.

Concerns:
- `admin`        — ACT-token gates, admin-only invocation
- `plan`         — soft (prompt injection) + hard (tool block) plan enforcement
- `privacy`      — A2A outbound secret-leak scanner
- `attachments`  — tool-emitted file_path → inline_data Part injection
- `core`         — prompt-injection scanner, token gate, throttle, model
                   hot-swap, output spillover, retry-cap, session state

Module-level state (`_throttle`, `_CACHED_VECTORS`) lives in `core` —
created once on import, shared across all callbacks that need it.
"""

from .admin import admin_only_guardrail, admin_tool_guardrail
from .attachments import (
    _FILE_ATTACHMENT_MARKER,
    _PENDING_FILE_PARTS_KEY,
    file_attachment_capture,
    file_attachment_inject,
)
from .bouncer import (
    force_bounce_before_model,
    on_tool_error_bouncer,
    reset_error_history_after_tool,
)
from .core import (
    prompt_injection_guardrail,
    state_setter,
    tool_output_injection_guardrail,
    tool_output_spillover_guardrail,
    verify_retry_guardrail,
)
from .plan import plan_enforcer, plan_step_enforcer
from .privacy import a2a_privacy_guardrail

__all__ = [
    "admin_only_guardrail",
    "admin_tool_guardrail",
    "a2a_privacy_guardrail",
    "file_attachment_capture",
    "file_attachment_inject",
    "force_bounce_before_model",
    "on_tool_error_bouncer",
    "plan_enforcer",
    "plan_step_enforcer",
    "prompt_injection_guardrail",
    "reset_error_history_after_tool",
    "state_setter",
    "tool_output_injection_guardrail",
    "tool_output_spillover_guardrail",
    "verify_retry_guardrail",
]
