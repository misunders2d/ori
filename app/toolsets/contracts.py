"""Contract pipeline toolset — author, validate, dry-run, freeze,
schedule, revise, list, inspect contracts.

Mounted on whichever sub-agent should own contract authoring (default:
CoordinatorAgent for now — contracts are a top-level workflow concept).
The set is intentionally small: every operation maps to one tool, and
the agent composes the contract dict itself rather than via dozens of
slot-setter tools.

Admin-gated where it matters (freeze, schedule, unschedule, revise) —
the agent will only be allowed to call these for the active admin
user; ``admin_tool_guardrail`` (``app/callbacks/guardrails.py``) is
the existing checkpoint that already enforces this for system tools.
"""

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool


class ContractToolset(BaseToolset):
    """Contract pipeline — author, dry-run, freeze, schedule scheduled
    tasks via the new contract-driven path (alternative to the legacy
    ``schedule_recurring_task`` flow).

    Use this for any recurring task whose content / format / channel
    needs to be precise. See ``docs/CONTRACTS.md`` for the authoring
    walkthrough.
    """

    async def get_tools(self, readonly_context=None):
        from app.tools.contracts import (
            contract_draft_validate,
            contract_dry_run,
            contract_freeze,
            contract_from_existing,
            contract_inspect,
            contract_list,
            contract_revise,
            contract_schedule,
            contract_unschedule,
        )

        return [
            FunctionTool(func=contract_draft_validate),
            FunctionTool(func=contract_dry_run),
            FunctionTool(func=contract_freeze),
            FunctionTool(func=contract_schedule),
            FunctionTool(func=contract_unschedule),
            FunctionTool(func=contract_revise),
            FunctionTool(func=contract_list),
            FunctionTool(func=contract_inspect),
            FunctionTool(func=contract_from_existing),
        ]
