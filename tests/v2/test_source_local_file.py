"""Phase 10 slice 3 — ``source_local_file`` + §5.3.1 fence.

Per ``docs/PHASE_10_PLAN.md`` §3.2 / §5. Exhaustive
bypass-vector matrix: every fence-evasion attempt is an
explicit rejected-test raising the NON-fallback
``SourceSecurityError`` / ``SourcePolicyError``; a
genuinely-absent contained file is the fallback-eligible
``SourceFetchError``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.v2.descriptors.source import SourceDescriptor
from app.v2.enums import SelectionMethod
from app.v2.registry import DuplicateDescriptorError, SourceRegistry
from app.v2.sources.contract import SourceLoader, content_hash_for
from app.v2.sources.errors import (
    SourceFetchError,
    SourceParseError,
    SourcePolicyError,
    SourceSecurityError,
)
from app.v2.sources.local_file import (
    SOURCE_LOCAL_FILE_ID,
    LocalFileSource,
    local_file_source,
    register_local_file,
)
from app.v2.sources.registry import SourceLoaderRegistry
from app.v2.tool_tags import ToolCapabilityTag


_T0 = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return _T0


def _src(root: Path, **kw) -> LocalFileSource:
    return LocalFileSource(allowed_roots=[root], **kw)


async def _load(src: LocalFileSource, path, **extra):
    return await src.load(
        args={"source_id": "in1", "path": str(path), **extra},
        as_of_datetime=None,
        clock=_clock,
    )


# ===========================================================================
# Happy paths
# ===========================================================================


@pytest.mark.asyncio
async def test_text_happy_path_verified_result(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    f = root / "note.txt"
    f.write_text("stand-up at 9")
    src = _src(root)

    r = await _load(src, f)
    assert r.content == "stand-up at 9"
    assert r.content_bytes == b"stand-up at 9"
    assert r.content_hash == content_hash_for(b"stand-up at 9")
    assert r.source_kind == SOURCE_LOCAL_FILE_ID
    assert r.source_id == "in1"
    assert r.item_count == 1
    assert r.selection_method is SelectionMethod.CONTENT_HASH
    assert r.fetched_at == _T0


@pytest.mark.asyncio
async def test_text_is_verbatim_newline_sensitive(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    a = root / "a.md"
    a.write_bytes(b"x")
    b = root / "b.md"
    b.write_bytes(b"x\n")
    src = _src(root)
    ra = await _load(src, a)
    rb = await _load(src, b)
    assert ra.content_bytes == b"x"
    assert rb.content_bytes == b"x\n"
    assert ra.content_hash != rb.content_hash


@pytest.mark.asyncio
async def test_json_is_canonicalised(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    f = root / "d.json"
    f.write_text('{"b":1,"a":2}')
    r = await _load(_src(root), f)
    assert r.content == {"a": 2, "b": 1}
    assert r.content_bytes == b'{"a":2,"b":1}'
    assert r.content_hash == content_hash_for(b'{"a":2,"b":1}')


@pytest.mark.asyncio
async def test_yaml_is_verbatim(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    f = root / "c.yaml"
    f.write_bytes(b"k: v\n")
    r = await _load(_src(root), f)
    assert r.content == "k: v\n"
    assert r.content_bytes == b"k: v\n"


@pytest.mark.asyncio
async def test_binary_opt_in(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    f = root / "blob.bin"
    f.write_bytes(bytes(range(32)))
    r = await _load(_src(root), f, allow_binary=True)
    assert r.content == {
        "_b64": __import__("base64")
        .b64encode(bytes(range(32)))
        .decode("ascii")
    }
    assert r.content_bytes == bytes(range(32))


# ===========================================================================
# Fence bypass-vector matrix — ALL SourceSecurityError,
# non-fallback (a denied read never serves a snapshot)
# ===========================================================================


def _assert_security(exc: SourceSecurityError):
    assert exc.fallback_eligible is False
    assert not isinstance(exc, SourceFetchError)


@pytest.mark.asyncio
async def test_empty_allowlist_denies_all(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    src = LocalFileSource(allowed_roots=())  # deny-all default
    with pytest.raises(SourceSecurityError) as ei:
        await _load(src, f)
    assert ei.value.payload_code == "path_outside_allowed_root"
    _assert_security(ei.value)


@pytest.mark.asyncio
async def test_sibling_prefix_bypass_rejected(tmp_path):
    """/safe/root_evil vs allowed /safe/root — the bug a
    startswith fence would pass; is_relative_to rejects."""
    root = tmp_path / "root"
    root.mkdir()
    sibling = tmp_path / "root_evil"
    sibling.mkdir()
    f = sibling / "secret.txt"
    f.write_text("pwned")
    with pytest.raises(SourceSecurityError) as ei:
        await _load(_src(root), f)
    assert ei.value.payload_code == "path_outside_allowed_root"
    _assert_security(ei.value)


@pytest.mark.asyncio
async def test_dotdot_traversal_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_text("x")
    traversal = root / ".." / "outside" / "s.txt"
    with pytest.raises(SourceSecurityError) as ei:
        await _load(_src(root), traversal)
    _assert_security(ei.value)


@pytest.mark.asyncio
async def test_absolute_escape_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "p.txt"
    outside.parent.mkdir()
    outside.write_text("x")
    with pytest.raises(SourceSecurityError) as ei:
        await _load(_src(root), outside)
    _assert_security(ei.value)


@pytest.mark.asyncio
async def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("secret")
    link = root / "link.txt"
    os.symlink(target, link)
    with pytest.raises(SourceSecurityError) as ei:
        await _load(_src(root), link)
    assert ei.value.payload_code == "path_outside_allowed_root"
    _assert_security(ei.value)


@pytest.mark.asyncio
async def test_symlink_into_deny_list_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "v.txt").write_text("creds")
    link = root / "shortcut.txt"
    os.symlink(blocked / "v.txt", link)
    # blocked/ is inside no allowed root anyway, but pin the
    # deny path explicitly: allow BOTH roots, deny blocked/.
    src = LocalFileSource(
        allowed_roots=[root, blocked],
        deny_dirs=[blocked],
    )
    with pytest.raises(SourceSecurityError) as ei:
        await _load(src, link)
    assert ei.value.payload_code == "path_in_deny_list"
    _assert_security(ei.value)


@pytest.mark.asyncio
async def test_deny_list_within_allowed_root_rejected(tmp_path):
    root = tmp_path / "root"
    (root / "secret").mkdir(parents=True)
    f = root / "secret" / "k.txt"
    f.write_text("creds")
    src = LocalFileSource(
        allowed_roots=[root], deny_dirs=[root / "secret"]
    )
    with pytest.raises(SourceSecurityError) as ei:
        await _load(src, f)
    assert ei.value.payload_code == "path_in_deny_list"
    _assert_security(ei.value)


@pytest.mark.asyncio
async def test_deny_name_glob_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    f = root / ".env"
    f.write_text("SECRET=1")
    # .env matches the default deny glob; deny check runs
    # BEFORE the mime check.
    with pytest.raises(SourceSecurityError) as ei:
        await _load(_src(root), f)
    assert ei.value.payload_code == "path_in_deny_list"
    _assert_security(ei.value)


@pytest.mark.asyncio
async def test_missing_target_symlink_escape_is_security_not_notfound(
    tmp_path,
):
    """A symlink whose target is OUTSIDE the root and does
    not exist must be classified SECURITY (non-fallback),
    NOT downgraded to fallback-eligible not_found."""
    root = tmp_path / "root"
    root.mkdir()
    missing_outside = tmp_path / "gone" / "nope.txt"  # never created
    link = root / "dangling.txt"
    os.symlink(missing_outside, link)
    with pytest.raises(SourceSecurityError) as ei:
        await _load(_src(root), link)
    _assert_security(ei.value)


# ===========================================================================
# TOCTOU — check and read MUST hit the same inode
# (codex slice-3 🔴 + 🟡)
# ===========================================================================


class _SwapToSymlinkAfterFence(LocalFileSource):
    """Simulates the race: the checked file is replaced by
    an out-of-root symlink AFTER _enforce_fence returns,
    before the loader opens it."""

    def __init__(self, *, swap_target: Path, **kw):
        super().__init__(**kw)
        self._swap_target = swap_target

    def _enforce_fence(self, resolved: Path) -> None:
        super()._enforce_fence(resolved)  # passes legitimately
        os.remove(resolved)
        os.symlink(self._swap_target, resolved)


class _SwapIntermediateDirAfterFence(LocalFileSource):
    """Simulates the DEEPER race: an INTERMEDIATE
    allowed-root directory component is replaced by an
    out-of-root symlink AFTER _enforce_fence, before the
    open. Final-component O_NOFOLLOW alone would NOT catch
    this — the dirfd component walk must."""

    def __init__(self, *, swap_dir: Path, evil_dir: Path, **kw):
        super().__init__(**kw)
        self._swap_dir = swap_dir
        self._evil_dir = evil_dir

    def _enforce_fence(self, resolved: Path) -> None:
        super()._enforce_fence(resolved)  # passes legitimately
        import shutil

        shutil.rmtree(self._swap_dir)
        os.symlink(self._evil_dir, self._swap_dir)


class _GrowAfterFence(LocalFileSource):
    """Simulates the race: the file grows past max_bytes
    AFTER the fence/stat, before the read."""

    def __init__(self, *, grow_to: int, **kw):
        super().__init__(**kw)
        self._grow_to = grow_to

    def _enforce_fence(self, resolved: Path) -> None:
        super()._enforce_fence(resolved)
        with open(resolved, "ab") as fh:
            fh.write(b"A" * self._grow_to)


@pytest.mark.asyncio
async def test_symlink_swap_after_fence_refuses(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    f = root / "ok.txt"
    f.write_text("legit")
    outside = tmp_path / "secret.txt"
    outside.write_text("PWNED")

    src = _SwapToSymlinkAfterFence(
        allowed_roots=[root], swap_target=outside
    )
    with pytest.raises(SourceSecurityError) as ei:
        await _load(src, f)
    assert ei.value.payload_code == "symlink_swapped_after_fence"
    assert ei.value.fallback_eligible is False
    assert not isinstance(ei.value, SourceFetchError)


@pytest.mark.asyncio
async def test_intermediate_dir_swap_after_fence_refuses(tmp_path):
    """Mid-path allowed-root dir swapped to an out-of-root
    symlink after the fence → the dirfd component walk's
    O_NOFOLLOW on the INTERMEDIATE component refuses
    (final-component O_NOFOLLOW alone would not)."""
    root = tmp_path / "root"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (sub / "leaf.txt").write_text("legit")

    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "leaf.txt").write_text("PWNED")

    src = _SwapIntermediateDirAfterFence(
        allowed_roots=[root], swap_dir=sub, evil_dir=evil
    )
    with pytest.raises(SourceSecurityError) as ei:
        await _load(src, sub / "leaf.txt")
    assert ei.value.payload_code == "symlink_swapped_after_fence"
    assert ei.value.fallback_eligible is False
    assert not isinstance(ei.value, SourceFetchError)


@pytest.mark.asyncio
async def test_grow_after_stat_refuses_non_fallback(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    f = root / "small.txt"
    f.write_bytes(b"hi")  # 2 bytes — under the limit at check time
    src = _GrowAfterFence(
        allowed_roots=[root], max_bytes=10, grow_to=100
    )
    with pytest.raises(SourcePolicyError) as ei:
        await _load(src, f)
    assert ei.value.payload_code == "source_local_file_oversize"
    assert ei.value.fallback_eligible is False


@pytest.mark.asyncio
async def test_oversize_giant_file_not_slurped(tmp_path):
    """The capped fd read must not pull the whole file into
    memory — reading max_bytes+1 then rejecting."""
    root = tmp_path / "root"
    root.mkdir()
    f = root / "big.txt"
    f.write_bytes(b"B" * 5000)
    src = LocalFileSource(allowed_roots=[root], max_bytes=100)
    with pytest.raises(SourcePolicyError) as ei:
        await _load(src, f)
    assert ei.value.payload_code == "source_local_file_oversize"
    assert ei.value.fallback_eligible is False


@pytest.mark.asyncio
async def test_directory_path_refused_non_regular(tmp_path):
    """A path that fstat-s as a non-regular file (dir)
    after the open is refused as SourceSecurityError."""
    root = tmp_path / "root"
    # allowlisted extension so the mime check passes and we
    # reach the post-open fstat regular-file check.
    (root / "d.txt").mkdir(parents=True)
    src = _src(root)
    with pytest.raises(SourceSecurityError) as ei:
        await _load(src, root / "d.txt")
    assert ei.value.payload_code == "path_not_a_regular_file"
    assert ei.value.fallback_eligible is False


# ===========================================================================
# Policy refusals — non-fallback SourcePolicyError
# ===========================================================================


@pytest.mark.asyncio
async def test_mime_not_allowlisted_rejected(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    f = root / "evil.py"
    f.write_text("print('x')")
    with pytest.raises(SourcePolicyError) as ei:
        await _load(_src(root), f)
    assert ei.value.payload_code == "mime_not_allowlisted"
    assert ei.value.fallback_eligible is False


@pytest.mark.asyncio
async def test_oversize_rejected_non_fallback(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    f = root / "big.txt"
    f.write_bytes(b"A" * 50)
    src = LocalFileSource(allowed_roots=[root], max_bytes=10)
    with pytest.raises(SourcePolicyError) as ei:
        await _load(src, f)
    assert ei.value.payload_code == "source_local_file_oversize"
    assert ei.value.fallback_eligible is False


# ===========================================================================
# Fetch (fallback-eligible) — contained but absent
# ===========================================================================


@pytest.mark.asyncio
async def test_contained_missing_file_is_fallback_eligible(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    missing = root / "not_here.txt"  # inside root, never created
    with pytest.raises(SourceFetchError) as ei:
        await _load(_src(root), missing)
    assert ei.value.payload_code == "source_local_file_not_found"
    assert ei.value.fallback_eligible is True


# ===========================================================================
# Parse (non-fallback)
# ===========================================================================


@pytest.mark.asyncio
async def test_bad_json_raises_parse_error(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    f = root / "x.json"
    f.write_text("{not json")
    with pytest.raises(SourceParseError) as ei:
        await _load(_src(root), f)
    assert ei.value.payload_code == "source_local_file_json_invalid"
    assert ei.value.fallback_eligible is False


@pytest.mark.asyncio
async def test_non_utf8_text_raises_parse_error(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    f = root / "x.txt"
    f.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(SourceParseError) as ei:
        await _load(_src(root), f)
    assert ei.value.payload_code == "source_local_file_decode_failed"
    assert ei.value.fallback_eligible is False


# ===========================================================================
# Args / descriptor / registration / protocol
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"path": "/x"},
        {"source_id": "i"},
        {"source_id": "", "path": "/x"},
        {"source_id": "i", "path": ""},
        {"source_id": "i", "path": 5},
    ],
)
async def test_bad_args_raise_parse_error(tmp_path, args):
    src = _src(tmp_path)
    with pytest.raises(SourceParseError) as ei:
        await src.load(args=args, as_of_datetime=None, clock=_clock)
    assert ei.value.payload_code == "source_local_file_args_invalid"
    assert ei.value.fallback_eligible is False


def test_descriptor_is_read_only_valid():
    d = local_file_source.descriptor
    assert isinstance(d, SourceDescriptor)
    assert d.id == SOURCE_LOCAL_FILE_ID
    assert ToolCapabilityTag.READ_EXTERNAL in d.tags
    assert not (
        d.tags
        & {
            ToolCapabilityTag.WRITE_EXTERNAL,
            ToolCapabilityTag.SEND_MESSAGE,
            ToolCapabilityTag.FILESYSTEM_WRITE,
        }
    )


def test_protocol_conformance():
    assert isinstance(local_file_source, SourceLoader)


def test_register_into_fresh_registries():
    sources = SourceRegistry()
    loaders = SourceLoaderRegistry()
    register_local_file(sources=sources, loaders=loaders)
    assert sources.lookup(SOURCE_LOCAL_FILE_ID) is (
        local_file_source.descriptor
    )
    assert loaders.require(SOURCE_LOCAL_FILE_ID) is local_file_source
    with pytest.raises(DuplicateDescriptorError):
        register_local_file(sources=sources, loaders=loaders)


def test_default_singleton_is_deny_all():
    """The production singleton has EMPTY allowed_roots —
    safe default until an admin opts a root in."""
    assert local_file_source._roots == ()


def test_builtin_registered_on_import():
    import app.v2.sources as src

    assert SOURCE_LOCAL_FILE_ID in src.SOURCE_LOADERS
    assert src.SOURCES.lookup(SOURCE_LOCAL_FILE_ID) is not None
