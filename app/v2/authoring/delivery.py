"""V2 scheduler — authoring delivery setter.

Phase 7 slice 3 per ``docs/PHASE_7_PLAN.md`` §3.4 + §5.4.

One tool — :func:`schedule_set_delivery` — that wires the
phase-6 registry cache resolver into the draft authoring
path. The setter funnels through:

1. ``cache = cache_loader()`` (DI'd — phase-9 cutover supplies
   ``load_cache(kind, expected_owner_id=..., base=...)``;
   tests pass a closure / stub).
2. If the cache is absent: optionally refresh via the DI'd
   :class:`SlackChannelsClient`. Refresh failures (or a
   missing client) surface as
   :meth:`ToolResponse.cache_unavailable` (round-2 reviewer
   Q8 — the ``NoCacheAndNetworkDown`` raise path closes
   here, not in :mod:`app.v2.registry_cache.refresh`).
3. :func:`resolve_channel(channel_lookup, cache)` — cache
   miss / channel-ambiguous → :meth:`ToolResponse.validation_failed`
   with a synthetic :class:`ValidationIssue`.
4. On success, mutate the draft so
   ``delivery.target_session_id = entry.id``, save, and
   run the shared :func:`_finalise` pipeline (re-run the
   full-spec validate when the draft is now complete).

The module imports NO ``slack_sdk`` / ``googleapiclient``
at module load — Protocol-typed DI (round-2 reviewer L347).
Production wrappers ship at phase 9 cutover.

References:
- ``docs/PHASE_7_PLAN.md`` §3.4 + §5.4
- ``docs/CONTRACTS_V2_DESIGN.md`` §5.7
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from app.v2.authoring.drafts import DraftStore, ScheduleSpecDraft
from app.v2.authoring.responses import ToolResponse
from app.v2.authoring.setters import _finalise, _validation_failed_single
from app.v2.enums import DeliveryFallbackPolicy
from app.v2.models.common import Delivery
from app.v2.registry_cache.errors import (
    CacheMiss,
    ChannelAmbiguous,
    NoCacheAvailable,
)
from app.v2.registry_cache.refresh import (
    SlackChannelsClient,
    refresh_slack_channels,
)
from app.v2.registry_cache.resolver import resolve_channel
from app.v2.registry_cache.schemas import SlackChannelsCache
from app.v2.validation import ValidationIssue


async def schedule_set_delivery(
    draft_id: str,
    *,
    channel_lookup: str,
    fallback_policy: DeliveryFallbackPolicy,
    session_id: str,
    store: DraftStore,
    cache_loader: Callable[[], Optional[SlackChannelsCache]],
    cache_saver: Callable[[SlackChannelsCache], None],
    slack_client: Optional[SlackChannelsClient],
    clock: Callable[[], datetime],
    expected_owner_id: str,
    include_archived: bool = False,
) -> ToolResponse:
    """Resolve a Slack channel + commit it to the draft's
    :class:`Delivery`.

    All DI args are REQUIRED (round-2 reviewer L365 / Q10);
    no env-derived defaults sneak into phase 7. The toolset
    (slice 6) constructs the loader / saver / client
    closures against its constructor args.

    Returns:
    - :meth:`ToolResponse.not_found` — draft missing.
    - :meth:`ToolResponse.cache_unavailable` — cache absent
      AND refresh failed (or no client). ``cache_kind`` is
      ``"slack_channels"``; ``network_error`` carries the
      cause.
    - :meth:`ToolResponse.validation_failed` — cache present
      but lookup miss / channel ambiguous.
    - :meth:`ToolResponse.ok` or :meth:`not_ready` — success
      (delegates to :func:`_finalise`).
    """
    try:
        draft = store.read(session_id, draft_id)
    except FileNotFoundError:
        return ToolResponse.not_found(
            message=f"draft {draft_id!r} not found"
        )

    cache: Optional[SlackChannelsCache] = cache_loader()

    # Cache absent path → optional refresh.
    if cache is None:
        if slack_client is None:
            return ToolResponse.cache_unavailable(
                kind="slack_channels",
                network_error="no slack_client configured",
            )
        try:
            cache = refresh_slack_channels(
                slack_client,
                expected_owner_id=expected_owner_id,
                clock=clock,
            )
        except Exception as exc:  # noqa: BLE001 — wrap any client failure
            return ToolResponse.cache_unavailable(
                kind="slack_channels",
                network_error=str(exc),
            )
        # Save the fresh cache via the DI'd saver (Q9).
        cache_saver(cache)

    # Resolve the channel.
    try:
        entry = resolve_channel(
            channel_lookup,
            cache,
            include_archived=include_archived,
        )
    except CacheMiss as exc:
        return ToolResponse.validation_failed(
            issues=[
                ValidationIssue(
                    code="channel_not_in_cache",
                    severity="error",
                    path="delivery.channel_lookup",
                    message=str(exc),
                )
            ]
        )
    except ChannelAmbiguous as exc:
        return ToolResponse.validation_failed(
            issues=[
                ValidationIssue(
                    code="channel_ambiguous",
                    severity="error",
                    path="delivery.channel_lookup",
                    message=(
                        f"{exc.name!r} matches multiple "
                        f"active channels: "
                        f"{exc.candidate_ids!r}"
                    ),
                )
            ]
        )
    except NoCacheAvailable:
        # Defence in depth — resolver shouldn't reach this
        # branch since we refreshed above, but pin via the
        # cache_unavailable shape so the LLM gets a
        # consistent answer.
        return ToolResponse.cache_unavailable(
            kind="slack_channels",
            network_error=(
                "resolver received cache=None despite "
                "refresh attempt"
            ),
        )

    delivery = Delivery(
        target_session_id=entry.id,
        fallback_policy=fallback_policy,
    )
    new_draft = draft.model_copy(update={"delivery": delivery})
    return _finalise(new_draft, session_id, store, clock)


__all__ = ["schedule_set_delivery"]
