"""Registry-aware semantic validation for contracts.

Pydantic's ``Contract.model_validate`` only checks types and shape. It
accepts any string for `EmitStep.adapter`, `Gate.type`, or
`InputSpec.loader` — even names that don't correspond to a registered
implementation. That's how the 2026-05-12 production failure happened:
five `ai_pilot_*_v2` contracts were authored with
``adapter: "slack_post_message"`` (the underlying tool name, not the
registered emit-adapter name ``slack_post``). They passed every
authoring check, were frozen, scheduled, and FATALed silently at
every fire because the adapter name was unknown.

This module closes that bug class. ``validate_against_registries`` is
called from the AUTHORING path only (``_coerce_spec`` in
``app.tools.contracts`` and ``contract_store.freeze``). On a bad name
it raises ``ValueError`` listing both the unknown name and the full
set of known names, so the bot can self-correct on the next attempt.

Why authoring-strict but load-lenient: if a registered adapter is
later renamed or removed, old frozen contracts still need to be
loadable for inspection and unscheduling. They'll FATAL at fire time
(with the now-clear "unknown adapter" message from the worker), but
``contract_inspect`` and ``contract_unschedule`` keep working.
"""

from __future__ import annotations

from app.contracts.schema import Contract


class RegistryValidationError(ValueError):
    """Raised when a contract references an unknown adapter / gate / loader."""

    pass


def _known_adapters() -> set[str]:
    # Lazy import: forces ``app.contracts.emit`` to load, which runs
    # every ``@register_adapter`` decorator and populates the registry.
    from app.contracts.emit import EMIT_ADAPTERS
    return set(EMIT_ADAPTERS.keys())


def _known_gates() -> set[str]:
    from app.contracts.emit import GATES
    return set(GATES.keys())


def _known_loaders() -> set[str]:
    from app.contracts.loaders import LOADERS
    return set(LOADERS.keys())


def validate_against_registries(contract: Contract) -> None:
    """Verify every adapter / gate / loader referenced by ``contract`` is
    registered. Raises ``RegistryValidationError`` on the first miss.

    Called from the AUTHORING path only:
      - ``_coerce_spec`` in ``app.tools.contracts`` (covers
        ``contract_draft_validate``, ``contract_dry_run``,
        ``contract_freeze``)
      - ``contract_store.freeze`` (defense-in-depth at the
        store-layer entry point)

    NOT called from ``contract_store.load`` — see module docstring.
    """
    errors: list[str] = []

    adapters = _known_adapters()
    for i, emit in enumerate(contract.emit):
        if emit.adapter not in adapters:
            errors.append(
                f"emit[{i}] references unknown adapter "
                f"{emit.adapter!r}. Known adapters: {sorted(adapters)}. "
                f"(The Slack tool function `slack_post_message` is NOT the "
                f"same as the emit adapter `slack_post` — adapters are "
                f"contract-layer wrappers.)"
            )

    gates = _known_gates()
    for i, emit in enumerate(contract.emit):
        if emit.gate is None:
            continue
        if emit.gate.type not in gates:
            errors.append(
                f"emit[{i}].gate.type = {emit.gate.type!r} is not a "
                f"registered gate. Known gates: {sorted(gates)}."
            )

    loaders = _known_loaders()
    for i, inp in enumerate(contract.inputs):
        if inp.loader not in loaders:
            errors.append(
                f"inputs[{i}] references unknown loader "
                f"{inp.loader!r}. Known loaders: {sorted(loaders)}."
            )

    if errors:
        raise RegistryValidationError(
            "Contract failed registry validation:\n  - "
            + "\n  - ".join(errors)
        )
