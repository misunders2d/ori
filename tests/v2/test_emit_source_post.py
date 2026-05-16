"""Phase 11 slice 5 — source content emit adapter.

Per ``docs/PHASE_11_PLAN.md`` §3.2 / §5 + the
claude-reviewer slice-5 hard-checks. Unit-level coverage
of :func:`app.v2.emit.source_post.emit_source_to_slack`
plus the no-SDK-at-module-load AST pin (mirrors the
phase-10 import-hygiene approach for this phase-11 NEW
module).
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app.v2.emit import source_post as sp_mod
from app.v2.emit.source_post import (
    SOURCE_POST_ADAPTER,
    SourcePostResult,
    emit_source_to_slack,
)
from app.v2.enums import EventKind
from app.v2.models.execution_plan import (
    EmitStep,
    ExecutionPlan,
    InputSpec,
)
from app.v2.models.source_ref import SourceRefSpec
from app.v2.models.common import LiveSourceCachePolicy
from app.v2.enums import LiveChangePolicy, SourceFallbackPolicy
from app.v2.sources.resolver import ResolveOutcome, ResolveStatus

_CHANNEL = "C012ABCDE"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _StubSlack:
    def __init__(self, response=None, raise_exc=None):
        self.calls: list[dict] = []
        self.response = (
            response
            if response is not None
            else {"ok": True, "ts": "1700000000.000100"}
        )
        self.raise_exc = raise_exc

    async def chat_postMessage(self, *, channel: str, text: str):
        if self.raise_exc is not None:
            raise self.raise_exc
        self.calls.append({"channel": channel, "text": text})
        return self.response


def _src_ref() -> SourceRefSpec:
    return SourceRefSpec(
        loader="source_literal",
        args={"source_id": "src", "text": "hello"},
        cache=LiveSourceCachePolicy(
            cache_ttl_seconds=300,
            stale_max_age_seconds=3600,
            fallback_policy=SourceFallbackPolicy.USE_LAST_GOOD_SNAPSHOT,
        ),
        live_change_policy=LiveChangePolicy.ALLOW,
    )


def _plan(
    *, channel=_CHANNEL, with_step=True, progress_strategy=None
) -> ExecutionPlan:
    emit = []
    if with_step:
        args = {}
        if channel is not None:
            args["channel"] = channel
        if progress_strategy is not None:
            args["progress_strategy"] = progress_strategy
        emit = [
            EmitStep(id="post", adapter=SOURCE_POST_ADAPTER, args=args)
        ]
    else:
        # A plan still needs >=1 emit (model invariant); use
        # a different adapter so the source_post lookup misses.
        emit = [EmitStep(id="other", adapter="slack_post", args={})]
    return ExecutionPlan(
        id="p_src",
        description="emit-adapter unit plan",
        author="tester",
        inputs=[
            InputSpec(id="src", loader="source_literal",
                      source_ref=_src_ref())
        ],
        reasoning=[],
        emit=emit,
    ).with_fresh_hash()


def _outcome(
    content_bytes=b"hello", changed_vs_prior=None
) -> ResolveOutcome:
    return ResolveOutcome(
        status=ResolveStatus.RESOLVED,
        event_kind=EventKind.SOURCE_RESOLVED,
        event_id="evt-1",
        event_emitted=True,
        source_id="src",
        content_bytes=content_bytes,
        content_hash="sha256:" + "a" * 64,
        changed_vs_prior=changed_vs_prior,
    )


async def _emit(resolved, *, plan=None, slack=None):
    return await emit_source_to_slack(
        spec=None,  # reserved param (phase parity); not read
        plan=plan if plan is not None else _plan(),
        resolved=resolved,
        slack_client=slack if slack is not None else _StubSlack(),
        clock=lambda: None,
    )


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_source_post_result_shape():
    r = SourcePostResult(ok=True, channel=_CHANNEL)
    assert r.ts is None and r.error is None
    assert r.skipped_unchanged is False  # slice-6 field, default False
    with pytest.raises(Exception):
        SourcePostResult(ok=True, channel=_CHANNEL, bogus=1)


# ---------------------------------------------------------------------------
# Happy path — VERBATIM bytes, channel from EmitStep args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emits_verbatim_and_ok():
    slack = _StubSlack()
    out = await _emit({"src": _outcome(b"line1\nline2\n")}, slack=slack)
    assert out.ok is True
    assert out.channel == _CHANNEL
    assert out.ts == "1700000000.000100"
    assert out.skipped_unchanged is False
    # Channel from the source_post EmitStep args; text is the
    # content_bytes decoded 1:1 — VERBATIM, no reformat.
    assert slack.calls == [
        {"channel": _CHANNEL, "text": "line1\nline2\n"}
    ]


@pytest.mark.asyncio
async def test_verbatim_unicode_bytes_preserved():
    slack = _StubSlack()
    payload = "héllo — 日本語\t✅".encode("utf-8")
    out = await _emit({"src": _outcome(payload)}, slack=slack)
    assert out.ok is True
    assert slack.calls[0]["text"] == payload.decode("utf-8")


# ---------------------------------------------------------------------------
# Failure shapes — never raises, uniform return
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_not_ok_maps_to_error():
    slack = _StubSlack(response={"ok": False, "error": "channel_not_found"})
    out = await _emit({"src": _outcome()}, slack=slack)
    assert out.ok is False
    assert out.error == "channel_not_found"


@pytest.mark.asyncio
async def test_slack_truthy_non_bool_not_ok():
    """`ok` must be the literal True bool — a truthy
    non-bool MUST NOT masquerade as success."""
    slack = _StubSlack(response={"ok": "true", "ts": "x"})
    out = await _emit({"src": _outcome()}, slack=slack)
    assert out.ok is False


@pytest.mark.asyncio
async def test_client_exception_wrapped_never_raised():
    slack = _StubSlack(raise_exc=RuntimeError("boom"))
    out = await _emit({"src": _outcome()}, slack=slack)
    assert out.ok is False
    assert out.error == "boom"


@pytest.mark.asyncio
async def test_missing_channel_in_emit_step_args():
    out = await _emit(
        {"src": _outcome()}, plan=_plan(channel=None)
    )
    assert out.ok is False
    assert "channel" in out.error


@pytest.mark.asyncio
async def test_no_source_post_emit_step():
    out = await _emit(
        {"src": _outcome()}, plan=_plan(with_step=False)
    )
    assert out.ok is False
    assert SOURCE_POST_ADAPTER in out.error


@pytest.mark.asyncio
async def test_no_content_bytes():
    bad = ResolveOutcome(
        status=ResolveStatus.RESOLVED,
        event_kind=EventKind.SOURCE_RESOLVED,
        event_id="e",
        event_emitted=True,
        source_id="src",
        content_bytes=None,
    )
    out = await _emit({"src": bad})
    assert out.ok is False
    assert "content_bytes" in out.error


@pytest.mark.asyncio
async def test_non_utf8_content_bytes():
    out = await _emit({"src": _outcome(b"\xff\xfe\x00bad")})
    assert out.ok is False
    assert "utf-8" in out.error


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [0, 2])
async def test_not_exactly_one_resolved_input(n):
    resolved = {f"s{i}": _outcome() for i in range(n)}
    out = await _emit(resolved)
    assert out.ok is False
    assert "exactly one" in out.error


# ---------------------------------------------------------------------------
# Q3 / hygiene — no vendor SDK at module load
# ---------------------------------------------------------------------------


_FORBIDDEN_MODULE_LOAD = {
    "app.v2.runtime._defaults",
    "slack_sdk",
    "google",
    "googleapiclient",
    "oauth2client",
    "httpx",
    "requests",
    "urllib3",
    "aiohttp",
}


class _ModuleScopeImports(ast.NodeVisitor):
    def __init__(self):
        self.names: set[str] = set()

    def visit_FunctionDef(self, node):  # noqa: N802
        return

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node):  # noqa: N802
        return

    def visit_Import(self, node):  # noqa: N802
        for a in node.names:
            self.names.add(a.name)

    def visit_ImportFrom(self, node):  # noqa: N802
        self.names.add(node.module or "")


def test_source_post_no_vendor_sdk_at_module_load():
    tree = ast.parse(inspect.getsource(sp_mod))
    c = _ModuleScopeImports()
    c.visit(tree)
    leaked = [
        i
        for i in c.names
        if any(
            i == bad or i.startswith(bad + ".")
            for bad in _FORBIDDEN_MODULE_LOAD
        )
    ]
    assert not leaked, (
        f"source_post leaks a forbidden module-load import "
        f"(Q3 — Protocol DI, no vendor SDK at module load): "
        f"{leaked!r}"
    )


def test_source_post_no_uuid_or_datetime_now_call():
    tree = ast.parse(inspect.getsource(sp_mod))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain: list[str] = []
        f = node.func
        while isinstance(f, ast.Attribute):
            chain.insert(0, f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            chain.insert(0, f.id)
        assert chain not in (
            ["uuid", "uuid4"],
            ["datetime", "now"],
        ), f"forbidden call {'.'.join(chain)}()"


# ---------------------------------------------------------------------------
# Slice 6 — progress_strategy skip_unchanged (§3.3 rule:
# EMIT unless changed_vs_prior is False; whole ALWAYS emits)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_unchanged_false_skips_no_slack_call():
    slack = _StubSlack()
    out = await _emit(
        {"src": _outcome(changed_vs_prior=False)},
        plan=_plan(progress_strategy="skip_unchanged"),
        slack=slack,
    )
    assert out.ok is True
    assert out.skipped_unchanged is True
    assert out.ts is None
    assert slack.calls == []  # NO Slack call on a no-op skip


@pytest.mark.asyncio
@pytest.mark.parametrize("cvp", [True, None])
async def test_skip_unchanged_emits_when_changed_or_none(cvp):
    """Rule: EMIT unless changed_vs_prior is False. True
    (changed / first-fire) AND None (FALLBACK_DEFAULT — a
    degraded default MUST surface, AI_EDITS r13) both
    POST."""
    slack = _StubSlack()
    out = await _emit(
        {"src": _outcome(b"hi", changed_vs_prior=cvp)},
        plan=_plan(progress_strategy="skip_unchanged"),
        slack=slack,
    )
    assert out.ok is True
    assert out.skipped_unchanged is False
    assert slack.calls == [{"channel": _CHANNEL, "text": "hi"}]


@pytest.mark.asyncio
async def test_whole_always_emits_even_when_unchanged():
    """progress_strategy='whole' (and the default) ALWAYS
    posts — changed_vs_prior is irrelevant."""
    slack = _StubSlack()
    out = await _emit(
        {"src": _outcome(b"x", changed_vs_prior=False)},
        plan=_plan(progress_strategy="whole"),
        slack=slack,
    )
    assert out.ok is True
    assert out.skipped_unchanged is False
    assert slack.calls == [{"channel": _CHANNEL, "text": "x"}]


@pytest.mark.asyncio
async def test_default_strategy_is_whole_emits_when_unchanged():
    slack = _StubSlack()
    out = await _emit(
        {"src": _outcome(b"x", changed_vs_prior=False)},
        plan=_plan(),  # no progress_strategy arg → "whole"
        slack=slack,
    )
    assert out.ok is True
    assert out.skipped_unchanged is False
    assert slack.calls == [{"channel": _CHANNEL, "text": "x"}]
