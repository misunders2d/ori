"""Fire-time worker: execute one contract end-to-end.

Walks GATHER → REASON → MATERIALIZE → VALIDATE → EMIT, captures a
complete audit trail, and applies side effects ONLY through emit
adapters. Reasoning steps run in isolation: the LLM is invoked
directly per step (not through the full agent Runner), and its output
is parsed against the step's schema with retry-on-failure feedback.

V1 scope — explicit boundaries so the user knows what to expect:

  - 0..N reasoning steps per contract, each with its own LLM call and
    its own structured-output validation. Works for the AI Pilot case
    (0 steps) and the FBA news digest case (1 step).
  - Sub-agent transfers within a single reasoning step are NOT yet
    wired. ``entry_agent`` is honoured for *model selection* (the
    agent's configured model + system prompt), but the LLM call is
    direct and the agent's tools, callbacks, and transfer plumbing
    are bypassed. This keeps V1 deterministic; the 30-step ASIN
    audit (which legitimately needs sub-agent transfers) is on the
    follow-up roadmap.
  - ``transfers_allowed`` and ``plan_step_enforcer`` integration: same
    follow-up.

What V1 DOES guarantee:

  - The contract hash on disk is verified before execution. Mismatch
    aborts the fire — no executing a tampered contract.
  - Inputs run in order; templates are rendered against accumulated
    state; loader names are validated.
  - Reasoning steps run in order; each step's output must validate
    against its output spec or the executor retries once with the
    validation error fed back to the model. Final failure routes
    through ``on_failure`` (no partial emit).
  - Emit adapters run in order; gates may skip individual emits or
    abort the whole fire.
  - Every fire writes a full audit JSONL line under
    ``data/contract_audit/<contract_id>/<fire_ts>.jsonl`` capturing
    inputs, reasoning IO, emit results, and any error.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import jsonschema

from app.contracts.emit import run_emit, run_gate
from app.contracts.loaders import run_loader
from app.contracts.schema import (
    Contract,
    EnforcementMode,
    OutputSpec,
    ReasoningStep,
)
from app.contracts.store import ContractHashMismatch, contract_store
from app.contracts.templating import TemplateError, render

logger = logging.getLogger(__name__)


_AUDIT_DIR = os.path.abspath("./data/contract_audit")


class ContractFireError(Exception):
    """A contract fire failed in a way that requires admin attention."""


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _audit_path(contract_id: str, fire_ts: str) -> str:
    return os.path.join(_AUDIT_DIR, contract_id, f"{fire_ts}.jsonl")


def _audit(path: str, event: dict) -> None:
    """Append one event line to the per-fire audit log. Best effort —
    audit failures must never interrupt the fire."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        with open(path, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("contract audit write failed (%s): %s", path, e)


# ---------------------------------------------------------------------------
# LLM invocation — minimal V1 path
# ---------------------------------------------------------------------------


async def _invoke_llm_for_step(
    step: ReasoningStep,
    rendered_user_text: str,
    previous_validation_error: Optional[str] = None,
) -> str:
    """Call the LLM for a reasoning step. V1 implementation: route via
    LiteLLM (Anthropic / Gemini / OpenRouter — same dispatch the
    agent tree uses) directly, skipping the full ADK runner.

    Returns the raw response TEXT. Caller is responsible for JSON
    parsing + schema validation. We deliberately don't use ADK's
    ``output_schema`` here because some providers reject schema +
    tools simultaneously, and we want a single code path that works
    across providers.
    """
    from app.app_utils.models import _parse_model_str, get_model_name

    model_str = step.model or get_model_name(step.entry_agent) or get_model_name(
        "CoordinatorAgent"
    )
    provider, model_name = _parse_model_str(model_str)

    system_prompt_parts = [
        f"You are executing reasoning step `{step.id}` of a frozen contract.",
        f"Description: {step.description or '(no description)'}",
        "You MUST return output that conforms to the contract's output specification.",
    ]

    if step.output.type == "json":
        system_prompt_parts.append(
            "Return ONLY a valid JSON object that satisfies the schema described "
            "in the user message. No prose, no markdown fences, no explanation — "
            "just the JSON object."
        )
    elif step.output.type == "text":
        if step.output.constraints:
            system_prompt_parts.append(
                "Constraints on your output: "
                + "; ".join(step.output.constraints)
            )

    if previous_validation_error:
        system_prompt_parts.append(
            f"PRIOR ATTEMPT FAILED VALIDATION: {previous_validation_error}\n"
            "Fix the issue and try again."
        )

    schema_hint = ""
    if step.output.type == "json" and step.output.schema_:
        schema_hint = (
            "\n\nReturn JSON matching this schema:\n"
            + json.dumps(step.output.schema_, indent=2)
        )

    user_message = rendered_user_text + schema_hint
    system_prompt = "\n\n".join(system_prompt_parts)

    # Route through LiteLLM for uniform provider handling. The
    # ``litellm`` package is already a top-level project dep; importing
    # it lazily keeps the contract module light when no fire is running.
    import litellm

    if provider == "google" and not model_str.startswith("openrouter"):
        # Direct Gemini call — LiteLLM's gemini path.
        completion_model = f"gemini/{model_name}"
    elif provider == "openrouter" or model_str.startswith("openrouter/"):
        # Strip the "openrouter/" prefix LiteLLM expects via model name.
        completion_model = model_str
    else:
        completion_model = model_str

    response = await litellm.acompletion(
        model=completion_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        temperature=0.2,
    )
    return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------


def _validate_output(
    raw_text: str, output: OutputSpec
) -> tuple[bool, Any, Optional[str]]:
    """Validate a step's raw text against its output spec.

    Returns ``(ok, parsed_value, error_message)``. On success
    ``error_message`` is None and ``parsed_value`` is the JSON object
    (for ``type=json``) or the trimmed string (for ``type=text``).
    """
    if output.type == "none":
        return True, None, None

    if output.type == "text":
        text = raw_text.strip()
        for c in output.constraints:
            ok, err = _check_text_constraint(text, c)
            if not ok:
                return False, None, err
        return True, text, None

    if output.type == "json":
        text = _strip_code_fences(raw_text).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            return False, None, (
                f"output is not valid JSON: {e}. Got: {text[:300]!r}"
            )

        if output.schema_:
            try:
                jsonschema.validate(instance=parsed, schema=output.schema_)
            except jsonschema.ValidationError as e:
                return False, None, f"output failed schema validation: {e.message}"

        return True, parsed, None

    return False, None, f"unknown output.type: {output.type!r}"


def _strip_code_fences(s: str) -> str:
    """LLMs love to wrap JSON in ```json fences even when told not to.
    Strip a single set if present."""
    s = s.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        # Drop first line (fence) and last line if it's a closing fence.
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        s = "\n".join(lines).strip()
    return s


def _check_text_constraint(text: str, constraint: str) -> tuple[bool, Optional[str]]:
    """Evaluate a simple text constraint. V1 supports a small set of
    predicates; advanced constraints are logged advisory-only."""
    c = constraint.lower().strip()
    if c.startswith("min ") and " chars" in c:
        try:
            n = int(c.split()[1])
            return (len(text) >= n, None if len(text) >= n else f"text shorter than {n} chars")
        except (ValueError, IndexError):
            return True, None  # malformed constraint = advisory
    if c.startswith("max ") and " chars" in c:
        try:
            n = int(c.split()[1])
            return (len(text) <= n, None if len(text) <= n else f"text longer than {n} chars")
        except (ValueError, IndexError):
            return True, None
    if c.startswith("contains "):
        needle = constraint.split(" ", 1)[1].strip().strip("\"'")
        return (
            needle in text,
            None if needle in text else f"missing required substring {needle!r}",
        )
    # Advisory — accept.
    return True, None


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


async def execute_contract(
    contract: Contract,
    dry_run: bool = False,
    mock_inputs: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Run a contract end-to-end.

    Args:
      contract: a frozen Contract loaded from the store.
      dry_run: if True, skip all emit side effects (gates still
        evaluate, emit args are still rendered, but adapters are not
        called). Loaders + reasoning run normally.
      mock_inputs: optional dict mapping input ids to fixed values,
        used in dry-runs to skip real fetches.

    Returns the final state dict (inputs + reasoning outputs + emit
    results). On failure, returns a dict with ``status='error'`` and
    an ``error`` message.
    """
    if contract.enforcement != EnforcementMode.STRICT:
        raise ContractFireError(
            f"contract {contract.id} is not in STRICT enforcement mode — "
            "refusing to fire. STRICT is the only production mode."
        )

    # Verify the on-disk body still matches the hash we were handed.
    # If the file was rewritten manually since this contract was loaded,
    # bail loudly rather than execute drifted content.
    try:
        verify = contract_store.load(contract.id, contract.hash)
        if verify.hash != contract.hash:
            raise ContractHashMismatch("hash drift between load and execute")
    except ContractHashMismatch as e:
        raise ContractFireError(f"contract integrity check failed: {e}") from e

    fire_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fire_id = f"{fire_ts}_{uuid.uuid4().hex[:8]}"
    audit_path = _audit_path(contract.id, fire_id)

    state: dict[str, Any] = {}
    state["__contract__"] = {
        "id": contract.id,
        "version": contract.version,
        "hash": contract.hash,
        "fire_id": fire_id,
        "dry_run": dry_run,
    }

    _audit(
        audit_path,
        {"phase": "fire_start", "contract_id": contract.id, "hash": contract.hash},
    )

    # ─── GATHER ─────────────────────────────────────────────────────────
    try:
        for inp in contract.inputs:
            if mock_inputs and inp.id in mock_inputs:
                state[inp.id] = mock_inputs[inp.id]
                _audit(audit_path, {"phase": "input", "id": inp.id, "source": "mock"})
                continue
            t0 = time.time()
            value = await run_loader(inp.loader, inp.args, state)
            state[inp.id] = value
            _audit(
                audit_path,
                {
                    "phase": "input",
                    "id": inp.id,
                    "loader": inp.loader,
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "size_repr": len(json.dumps(value, default=str)),
                },
            )
    except (TemplateError, KeyError, ValueError, RuntimeError) as e:
        _audit(audit_path, {"phase": "input_failed", "error": str(e)})
        return await _on_failure(contract, audit_path, f"input loader failed: {e}")

    # ─── REASON ─────────────────────────────────────────────────────────
    try:
        for step in contract.reasoning:
            t0 = time.time()
            rendered_user_text = render(step.user_template, state)

            attempt = 0
            max_attempts = 1 + step.retry.on_validation_fail
            previous_error: Optional[str] = None
            parsed: Any = None

            while attempt < max_attempts:
                raw = await _invoke_llm_for_step(
                    step, rendered_user_text, previous_validation_error=previous_error
                )
                ok, parsed, err = _validate_output(raw, step.output)
                if ok:
                    break
                _audit(
                    audit_path,
                    {
                        "phase": "reasoning_retry",
                        "step": step.id,
                        "attempt": attempt + 1,
                        "error": err,
                    },
                )
                previous_error = err
                attempt += 1

            if attempt >= max_attempts and not (parsed is not None or step.output.type == "none"):
                _audit(
                    audit_path,
                    {
                        "phase": "reasoning_failed",
                        "step": step.id,
                        "final_error": previous_error,
                    },
                )
                return await _on_failure(
                    contract,
                    audit_path,
                    f"reasoning step {step.id!r} failed validation after "
                    f"{max_attempts} attempts: {previous_error}",
                )

            state[step.id] = parsed if step.output.type != "none" else None
            _audit(
                audit_path,
                {
                    "phase": "reasoning",
                    "step": step.id,
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "attempts": attempt + 1,
                },
            )
    except TemplateError as e:
        _audit(audit_path, {"phase": "reasoning_template_failed", "error": str(e)})
        return await _on_failure(contract, audit_path, f"reasoning template error: {e}")

    # ─── EMIT ───────────────────────────────────────────────────────────
    emit_results: list[dict] = []
    try:
        for emit in contract.emit:
            # Gate
            if emit.gate is not None:
                try:
                    gate_passed = await run_gate(
                        emit.gate.type, emit.gate.args, state
                    )
                except Exception as ge:
                    gate_passed = False
                    _audit(
                        audit_path,
                        {"phase": "emit_gate_error", "emit": emit.id, "error": str(ge)},
                    )

                if not gate_passed:
                    _audit(
                        audit_path,
                        {
                            "phase": "emit_gate_blocked",
                            "emit": emit.id,
                            "adapter": emit.adapter,
                        },
                    )
                    if emit.abort_on_gate_fail:
                        return await _on_failure(
                            contract,
                            audit_path,
                            f"emit {emit.id or emit.adapter!r} gate failed and "
                            f"abort_on_gate_fail=true",
                        )
                    continue

            # Adapter
            if dry_run:
                # Render args so the dry-run preview shows what WOULD post.
                rendered = render(emit.args, state)
                emit_results.append(
                    {
                        "id": emit.id,
                        "adapter": emit.adapter,
                        "dry_run": True,
                        "rendered_args": rendered,
                    }
                )
                _audit(
                    audit_path,
                    {"phase": "emit_skipped_dry_run", "emit": emit.id, "adapter": emit.adapter},
                )
                continue

            try:
                result = await run_emit(emit.adapter, emit.args, state)
                emit_results.append({"id": emit.id, "adapter": emit.adapter, "result": result})
                _audit(
                    audit_path,
                    {"phase": "emit", "emit": emit.id, "adapter": emit.adapter, "ok": True},
                )
            except Exception as ee:
                _audit(
                    audit_path,
                    {
                        "phase": "emit_failed",
                        "emit": emit.id,
                        "adapter": emit.adapter,
                        "error": str(ee),
                    },
                )
                return await _on_failure(
                    contract,
                    audit_path,
                    f"emit {emit.id or emit.adapter!r} failed: {ee}",
                )
    finally:
        _audit(audit_path, {"phase": "fire_end", "emit_count": len(emit_results)})

    state["__emit_results__"] = emit_results
    state["__status__"] = "ok"
    return state


# ---------------------------------------------------------------------------
# Failure routing
# ---------------------------------------------------------------------------


async def _on_failure(
    contract: Contract, audit_path: str, error: str
) -> dict[str, Any]:
    """Apply ``contract.on_failure`` policy and return an error state dict."""
    cfg = contract.on_failure
    _audit(audit_path, {"phase": "on_failure", "action": cfg.action.value, "error": error})

    if cfg.action.value == "alert_admin" and cfg.notify:
        from app.contracts.emit import run_emit

        for user_id in cfg.notify:
            try:
                await run_emit(
                    "telegram_dm",
                    {
                        "user_id": user_id,
                        "text": (
                            f"Contract `{contract.id}` v{contract.version} failed:\n"
                            f"{error}\n\nAudit: {audit_path}"
                        ),
                    },
                    {},
                )
            except Exception as e:
                logger.warning("alert_admin failed to notify %s: %s", user_id, e)

    return {"__status__": "error", "__error__": error, "__audit__": audit_path}
