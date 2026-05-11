"""Contract-driven scheduling — wildly different per task, but each one
authored once with a bot-drafted YAML, frozen with a hash, then executed
literally.

Public surface lives in ``app.tools.contracts`` (admin-gated tool calls);
this package holds the implementation: schema, storage, loaders, worker,
emit adapters, executor.

See ``docs/CONTRACTS.md`` for the full design rationale and authoring
walkthrough.
"""

from app.contracts.schema import (
    Contract,
    ContractVersion,
    InputSpec,
    ReasoningStep,
    EmitStep,
    Trigger,
    Acceptance,
    OutputSpec,
    Gate,
    Retry,
    FailureAction,
    EnforcementMode,
)
from app.contracts.store import (
    ContractStore,
    ContractHashMismatch,
    ContractNotFound,
    contract_store,  # module-level singleton
)

__all__ = [
    "Contract",
    "ContractVersion",
    "InputSpec",
    "ReasoningStep",
    "EmitStep",
    "Trigger",
    "Acceptance",
    "OutputSpec",
    "Gate",
    "Retry",
    "FailureAction",
    "EnforcementMode",
    "ContractStore",
    "ContractHashMismatch",
    "ContractNotFound",
    "contract_store",
]
