"""ToolDescriptor — registry shape for any v2-aware tool.

A ToolDescriptor is the metadata record the runtime guard
consults before invoking a tool. It carries the tool's name,
description, capability tags, and importable module path.

There is no executor reference here — that's a runtime concern
introduced in a later phase. Phase 2 ships the descriptor shape
so adapter authors can populate static registries.

References:
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.4
- ``docs/PHASE_2_PLAN.md`` §4.1
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.v2.tool_tags import ToolCapabilityTag


_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolDescriptor(BaseModel):
    """Static registry record for a tool / loader / adapter.

    ``name`` is the snake_case identifier the agent uses in
    invocations. It must match the actual registered tool name
    1:1; the runtime guard refuses unknown names.

    ``tags`` MUST be non-empty. The design contract's default
    for unknown tools is ``write_external`` (fail-safe), but
    the descriptor itself never carries an implicit default —
    callers spell out the intended tag set explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=8)
    tags: set[ToolCapabilityTag] = Field(min_length=1)
    module: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _name_is_snake_case(cls, v: str) -> str:
        if not _SNAKE_CASE.match(v):
            raise ValueError(
                f"ToolDescriptor.name must be snake_case "
                f"(^[a-z][a-z0-9_]*$); got {v!r}"
            )
        return v


__all__ = ["ToolDescriptor"]
