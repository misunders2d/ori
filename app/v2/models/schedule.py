"""V2 scheduler — ScheduleSpec (the root persisted shape).

A ScheduleSpec is the user's intent: when to fire, who owns it,
where the output goes, what to do on failure, optionally a
hash-pinned ExecutionPlan reference for multi-step workflows.

Lightweight reminders (use cases 6, 11, 13 in the design
contract) leave ``execution_plan_hash`` as None — they have no
workflow body to freeze. Heavy workflows (FBA audit, recurring
series) reference an ExecutionPlan by hash.

Hash semantics: ``ScheduleSpec.hash`` is SHA-256 over the
canonicalised body excluding ``hash`` and ``authored_at``. Two
ScheduleSpecs with identical authoring content produce identical
hashes. Any byte change (description tweak, trigger edit,
delivery rerouting) produces a new hash = new version.

See ``docs/CONTRACTS_V2_DESIGN.md`` §4.0 and ``docs/PHASE_1_PLAN.md``
§4.4.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.v2.enums import ScheduleStatus
from app.v2.models.common import (
    AuditPolicy,
    Delivery,
    FailurePolicy,
    TemplateRef,
    UserRef,
)
from app.v2.models.triggers import Trigger


_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_MIN_DESCRIPTION_LENGTH = 8


def _utc_now_iso() -> str:
    """ISO 8601 UTC timestamp for ``authored_at`` defaults."""
    return datetime.now(timezone.utc).isoformat()


class ScheduleSpec(BaseModel):
    """A persisted scheduling intent. Root object of the v2
    scheduler. References an optional frozen ExecutionPlan by
    hash for multi-step workflows; reminders leave that field
    None.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description=(
            "Globally unique snake_case identifier. Used as the "
            "primary key in the `schedules` table and the seed "
            "for APScheduler job ids. Must match "
            "^[a-z][a-z0-9_]*$ — starts with a lowercase letter, "
            "only lowercase letters / digits / underscores."
        ),
    )
    owner: UserRef
    description: str = Field(
        description=(
            "Human-readable summary of what this schedule does. "
            "Must be at least 8 characters so observability tools "
            "have something useful to display."
        ),
    )
    trigger: Trigger
    delivery: Delivery
    failure: FailurePolicy
    audit: AuditPolicy
    status: ScheduleStatus = ScheduleStatus.ACTIVE
    execution_plan_hash: Optional[str] = Field(
        default=None,
        description=(
            "SHA-256 hash of a frozen ExecutionPlan. None for "
            "lightweight reminders (one-off DMs, simple "
            "follow-ups). Required for any ScheduleSpec that "
            "needs structured multi-step work — that's what the "
            "ExecutionPlan body is for."
        ),
    )
    template: Optional[TemplateRef] = Field(
        default=None,
        description=(
            "Name + version of the template that produced this "
            "ScheduleSpec. None for CustomFlow specs."
        ),
    )
    authored_at: str = Field(
        default_factory=_utc_now_iso,
        description=(
            "ISO 8601 UTC timestamp. Excluded from the hash "
            "computation so re-running an idempotent author "
            "flow produces the same hash."
        ),
    )
    parent_hash: Optional[str] = Field(
        default=None,
        description=(
            "Hash of the prior ScheduleSpec this revision "
            "descended from. None for the initial creation. "
            "Forms the revision chain — same id, different "
            "hashes linked by parent_hash."
        ),
    )
    hash: str = Field(
        default="",
        description=(
            "SHA-256 of the canonicalised body. Empty on draft; "
            "populated by `compute_hash` / `with_fresh_hash` at "
            "freeze time."
        ),
    )

    # ---- validators ----

    @field_validator("id")
    @classmethod
    def _id_pattern(cls, v: str) -> str:
        if not _ID_PATTERN.fullmatch(v):
            raise ValueError(
                "id must match ^[a-z][a-z0-9_]*$ — starts with a "
                "lowercase letter; only lowercase letters / digits "
                "/ underscores allowed."
            )
        return v

    @field_validator("description")
    @classmethod
    def _description_min_length(cls, v: str) -> str:
        if len(v.strip()) < _MIN_DESCRIPTION_LENGTH:
            raise ValueError(
                f"description must be at least "
                f"{_MIN_DESCRIPTION_LENGTH} characters (after "
                "whitespace strip)."
            )
        return v

    # ---- hashing ----

    def canonical_body(self) -> dict[str, Any]:
        """Return the JSON-serialisable dict used for hash
        computation. Excludes ``hash`` and ``authored_at`` so
        re-runs of an idempotent author produce stable hashes.
        """
        d = self.model_dump(mode="json", by_alias=True)
        d.pop("hash", None)
        d.pop("authored_at", None)
        return d

    def compute_hash(self) -> str:
        """SHA-256 of the canonical JSON body."""
        body = self.canonical_body()
        encoded = json.dumps(
            body, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def with_fresh_hash(self) -> "ScheduleSpec":
        """Return a copy of self with the ``hash`` field populated
        from ``compute_hash()``. Used at freeze time."""
        return self.model_copy(update={"hash": self.compute_hash()})
