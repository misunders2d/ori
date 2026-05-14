"""Phase-2 descriptor contracts.

Each module here exposes a registry shape (``*Descriptor``) plus
the request / response Pydantic models the adapter author
implements:

- ``tool.py``    — generic ToolDescriptor for any callable.
- ``source.py``  — SourceDescriptor + SourceInputContract +
  SourceOutputContract for read-only loaders.
- ``emit.py``    — EmitDescriptor + EmitInputContract +
  EmitOutputContract for side-effecting deliveries.

Phase 2 is the contract surface only. Concrete adapter
implementations land in later phases (the design contract's
§12 step 9 onward).
"""
