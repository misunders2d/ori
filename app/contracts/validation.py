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

import re
from typing import Any, Iterator

from app.contracts.schema import Contract, EnforcementMode


class RegistryValidationError(ValueError):
    """Raised when a contract references an unknown adapter / gate / loader."""

    pass


class RigorValidationError(ValueError):
    """Raised when a contract author submits a spec that would allow the
    fire-time LLM to deviate from declared steps — e.g. an unconstrained
    output schema, a single reasoning step that smuggles multiple
    substeps, a template referencing data that no loader pulls, or
    ``enforcement=permissive``. See ``validate_step_rigor``.
    """

    pass


# Match ``{id}`` and ``{id.path.like.this}``. The root id must be
# snake_case (matches ``InputSpec.id`` / ``ReasoningStep.id`` patterns).
_PLACEHOLDER_RE = re.compile(r"\{([a-z][a-z0-9_]*)(?:\.[^}]+)?\}")

# Patterns that indicate multiple substeps smuggled into a single
# ``user_template``. Each match is one numbered/keyword step.
_NUMBERED_SUBSTEP_RE = re.compile(r"(?:^|\n)\s*\d+\s*[.)]\s+\S", re.MULTILINE)
_KEYWORD_SUBSTEP_RE = re.compile(r"(?:^|\n)\s*[Ss]tep\s+\d+\s*[:.]", re.MULTILINE)

# Mechanical text constraint prefixes that ``_check_text_constraint`` in
# ``app.contracts.worker`` actually enforces. Anything else is advisory.
_MECHANICAL_TEXT_CONSTRAINT_PREFIXES = ("min ", "max ", "contains ")


def _placeholder_ids(text: str) -> set[str]:
    """Return the set of root placeholder ids referenced inside ``text``.

    ``{sales_30d}`` → ``{"sales_30d"}``;
    ``{sales_30d.revenue.usd}`` → ``{"sales_30d"}``.
    """
    return {m.group(1) for m in _PLACEHOLDER_RE.finditer(text)}


def _walk_string_values(obj: Any) -> Iterator[str]:
    """Yield every string nested anywhere inside ``obj`` (dict / list /
    scalar). Used to scan loader / emit / gate args for placeholders."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_string_values(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_string_values(v)


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


def validate_step_rigor(contract: Contract) -> None:
    """Reject contract specs that would let the LLM deviate at fire time.

    The contract executor (``app.contracts.worker``) is mechanical — it
    runs declared steps in declared order, validates each step's output
    against its declared schema, and aborts on failure. That guarantee
    only matters if the *declared* spec is itself rigorous. A reasoning
    step with ``output.type="none"``, an empty json schema, or a
    multi-substep prompt collapses the guarantee down to "the LLM
    decides".

    This validator runs at AUTHORING time (``_coerce_spec`` in
    ``app.tools.contracts``) and blocks the freeze for the following
    classes of sloppy spec:

      1. ``enforcement=permissive`` — production-forbidden. The freeze
         path must refuse it loudly, not silently downgrade.
      2. ``ReasoningStep.output.type="none"`` — a step that doesn't
         produce checkable output. The worker can't validate or retry it.
      3. ``ReasoningStep.output.type="json"`` with empty / placeholder
         schema (no ``properties`` / ``items`` / ``$ref``, or all-optional
         when ``properties`` are present). A schema that constrains
         nothing constrains nothing.
      4. ``ReasoningStep.output.type="text"`` with zero mechanical
         constraints. Advisory predicates are logged but not enforced;
         a step needs at least one of ``min N chars`` / ``max N chars``
         / ``contains "..."``.
      5. ``user_template`` that contains >=3 numbered/keyword substeps
         (``1. ...`` / ``Step 2: ...``). That is a multi-step task
         smuggled into a single reasoning step — each substep should
         be its own ``ReasoningStep`` with its own schema-validated
         output.
      6. ``{placeholder.path}`` references that don't resolve to a
         declared input id or earlier reasoning-step id. Catches
         "the prompt asks for data nobody pulled" sloppiness at author
         time instead of producing hallucinated outputs at fire time.

    Visibility rules for placeholder resolution:

      - ``ReasoningStep.user_template`` may reference any
        ``InputSpec.id`` or any ``ReasoningStep.id`` declared *before*
        this step in the list.
      - ``InputSpec.args`` may reference any other ``InputSpec.id``
        (loader chaining — strict declaration-order is the worker's
        problem, not this validator's).
      - ``EmitStep.args``, ``EmitStep.gate.args``, and
        ``Acceptance.checks`` may reference any input id or reasoning
        step id (they all run after reasoning is complete).

    Raises ``RigorValidationError`` listing every issue (not just the
    first) so the author can fix all of them in one pass.
    """
    errors: list[str] = []

    if contract.enforcement == EnforcementMode.PERMISSIVE:
        errors.append(
            "enforcement='permissive' is authoring-experiment only; "
            "production freezes must use 'strict'. Set "
            "enforcement='strict'."
        )

    declared_inputs: set[str] = {inp.id for inp in contract.inputs}
    declared_steps_so_far: set[str] = set()

    for i, step in enumerate(contract.reasoning):
        prefix = f"reasoning[{i}] (id={step.id!r})"

        # ---- Output rigor ----
        out = step.output
        if out.type == "none":
            errors.append(
                f"{prefix}: output.type='none' is not allowed. The worker "
                "cannot validate or retry a step with no output. Set "
                "output.type='json' (with a schema) or 'text' (with "
                "mechanical constraints)."
            )
        elif out.type == "json":
            sch = out.schema_
            if not sch or not isinstance(sch, dict):
                errors.append(
                    f"{prefix}: output.type='json' requires a non-empty "
                    "schema_ object."
                )
            elif (
                "properties" not in sch
                and "items" not in sch
                and "$ref" not in sch
            ):
                errors.append(
                    f"{prefix}: output schema must declare 'properties' "
                    "(for objects), 'items' (for arrays), or '$ref'. Got "
                    f"keys: {sorted(sch.keys())!r}. An empty schema "
                    "validates anything."
                )
            elif "properties" in sch:
                required = sch.get("required") or []
                if not isinstance(required, list) or not required:
                    errors.append(
                        f"{prefix}: json schema lists 'properties' but no "
                        "'required' field. An all-optional schema lets "
                        "the LLM omit every field. Name at least one "
                        "required key."
                    )
        elif out.type == "text":
            mechanical = [
                c
                for c in out.constraints
                if c.lower().lstrip().startswith(
                    _MECHANICAL_TEXT_CONSTRAINT_PREFIXES
                )
            ]
            if not mechanical:
                errors.append(
                    f"{prefix}: output.type='text' requires at least one "
                    "mechanical constraint (one of "
                    f"{list(_MECHANICAL_TEXT_CONSTRAINT_PREFIXES)!r}). "
                    "Advisory predicates are logged but not enforced; "
                    "without a mechanical constraint the worker accepts "
                    "any string the LLM returns."
                )

        # ---- Substep smuggling ----
        user_template = step.user_template
        numbered = _NUMBERED_SUBSTEP_RE.findall(user_template)
        keyword = _KEYWORD_SUBSTEP_RE.findall(user_template)
        smuggled = max(len(numbered), len(keyword))
        if smuggled >= 3:
            errors.append(
                f"{prefix}: user_template contains {smuggled} numbered or "
                "'Step N:' substeps. That is a multi-step task collapsed "
                "into one reasoning step — split it into separate "
                "ReasoningStep entries so each gets its own "
                "schema-validated output."
            )

        # ---- Placeholder resolution ----
        visible_here = declared_inputs | declared_steps_so_far
        for ph in _placeholder_ids(user_template):
            if ph not in visible_here:
                errors.append(
                    f"{prefix}: user_template references "
                    f"{{{ph}.*}} but no input or earlier reasoning step "
                    f"has id={ph!r}. Visible at this step: "
                    f"{sorted(visible_here) or '(none)'}."
                )

        declared_steps_so_far.add(step.id)

    # Emit / acceptance / gates can see every declared input and every
    # reasoning step (reasoning is complete before any emit fires).
    visible_all: set[str] = declared_inputs | declared_steps_so_far

    for i, inp in enumerate(contract.inputs):
        for s in _walk_string_values(inp.args):
            for ph in _placeholder_ids(s):
                if ph not in declared_inputs:
                    errors.append(
                        f"inputs[{i}] (id={inp.id!r}): args reference "
                        f"{{{ph}.*}} but no input has id={ph!r}. Inputs "
                        "may chain only across declared loaders; reasoning "
                        f"step ids are not visible here. Declared inputs: "
                        f"{sorted(declared_inputs) or '(none)'}."
                    )

    for i, emit in enumerate(contract.emit):
        label = f"emit[{i}] (adapter={emit.adapter!r})"
        for s in _walk_string_values(emit.args):
            for ph in _placeholder_ids(s):
                if ph not in visible_all:
                    errors.append(
                        f"{label}: args reference {{{ph}.*}} but no "
                        f"input or reasoning step has id={ph!r}. "
                        f"Visible: {sorted(visible_all) or '(none)'}."
                    )
        if emit.gate is not None:
            for s in _walk_string_values(emit.gate.args):
                for ph in _placeholder_ids(s):
                    if ph not in visible_all:
                        errors.append(
                            f"{label}.gate (type={emit.gate.type!r}): "
                            f"args reference {{{ph}.*}} but no input or "
                            f"reasoning step has id={ph!r}. Visible: "
                            f"{sorted(visible_all) or '(none)'}."
                        )

    for check in contract.acceptance.checks:
        for ph in _placeholder_ids(check):
            if ph not in visible_all:
                errors.append(
                    f"acceptance.checks references {{{ph}.*}} but no "
                    f"input or reasoning step has id={ph!r}. Visible: "
                    f"{sorted(visible_all) or '(none)'}."
                )

    if errors:
        raise RigorValidationError(
            "Contract failed rigor validation:\n  - "
            + "\n  - ".join(errors)
        )
