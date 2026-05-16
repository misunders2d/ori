"""Phase 10 slice 7 — ``source_drive_file`` loader.

Per ``docs/PHASE_10_PLAN.md`` §1.1 / §3.5 / §5 + Q3. Fake
:class:`DriveFileReader` (no live Drive / no real OAuth);
the Drive error-code → typed-taxonomy map pinned with the
slice-5 non-fallback / fallback classification. The OAuth
/ cred path is the SAME Protocol-DI mechanism the registry
cache's ``GoogleDriveClient`` uses (no google SDK in the
loader path; no new credential mechanism).
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone

import pytest

from app.v2.descriptors.source import SourceDescriptor
from app.v2.enums import SelectionMethod
from app.v2.registry import DuplicateDescriptorError, SourceRegistry
from app.v2.sources import drive_file as drive_mod
from app.v2.sources.contract import SourceLoader, content_hash_for
from app.v2.sources.drive_file import (
    SOURCE_DRIVE_FILE_ID,
    DriveFileReader,
    DriveFileSource,
    drive_file_source,
    register_drive_file,
)
from app.v2.sources.errors import (
    SourceAuthError,
    SourceFetchError,
    SourceParseError,
    SourcePolicyError,
)
from app.v2.sources.registry import SourceLoaderRegistry
from app.v2.tool_tags import ToolCapabilityTag


_T0 = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return _T0


class _FakeReader:
    def __init__(self, *, resp=None, raises=None):
        self.calls = 0
        self._resp = resp
        self._raises = raises

    async def read_file(self, *, file_id):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._resp


def _src(reader) -> DriveFileSource:
    return DriveFileSource(reader=reader)


async def _load(src, **extra):
    args = {"source_id": "in1", "file_id": "F123", **extra}
    return await src.load(args=args, as_of_datetime=None, clock=_clock)


# ===========================================================================
# Happy path — kinds + revision → source_version
# ===========================================================================


@pytest.mark.asyncio
async def test_happy_text_with_revision():
    reader = _FakeReader(
        resp={
            "ok": True,
            "kind": "text",
            "content": "doc body\n",
            "revision": "rev-42",
        }
    )
    r = await _load(_src(reader))
    assert r.content == "doc body\n"
    assert r.content_bytes == b"doc body\n"
    assert r.content_hash == content_hash_for(b"doc body\n")
    assert r.source_kind == SOURCE_DRIVE_FILE_ID
    assert r.source_id == "in1"
    assert r.source_version == "rev-42"
    assert r.selection_method is SelectionMethod.STABLE_ID
    assert r.item_count == 1
    assert r.fetched_at == _T0


@pytest.mark.asyncio
async def test_happy_json_canonicalised_no_revision():
    reader = _FakeReader(
        resp={"ok": True, "kind": "json", "content": {"b": 1, "a": 2}}
    )
    r = await _load(_src(reader))
    assert r.content == {"b": 1, "a": 2}
    assert r.content_bytes == b'{"a":2,"b":1}'
    assert r.source_version is None


@pytest.mark.asyncio
async def test_happy_binary_kind():
    blob = bytes(range(16))
    reader = _FakeReader(
        resp={"ok": True, "kind": "binary", "content": blob}
    )
    r = await _load(_src(reader))
    assert r.content_bytes == blob


@pytest.mark.asyncio
async def test_list_content_item_count():
    reader = _FakeReader(
        resp={"ok": True, "kind": "json", "content": [1, 2, 3]}
    )
    r = await _load(_src(reader))
    assert r.item_count == 3


# ===========================================================================
# Drive error-code → typed taxonomy + fallback classification
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        # OAuth / token / authentication
        "invalid_grant",
        "unauthorized",
        "unauthenticated",
        "authError",
        "tokenExpired",
        # 403 permission-class reasons (codex slice-7 🔴 —
        # these previously fell through to fallback-eligible
        # SourceFetchError; now non-fallback auth)
        "forbidden",
        "insufficientPermissions",
        "permissionDenied",
        "insufficientFilePermissions",
        "appNotAuthorizedToFile",
        "domainPolicy",
        # unregistered / no-credentials (auth-origin)
        "dailyLimitExceededUnreg",
    ],
)
async def test_auth_errors_non_fallback(error):
    reader = _FakeReader(resp={"ok": False, "error": error})
    with pytest.raises(SourceAuthError) as ei:
        await _load(_src(reader))
    assert ei.value.fallback_eligible is False
    assert ei.value.payload_code == f"drive_{error}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "quotaExceeded",
        "backendError",
        "internalError",
        "serviceUnavailable",
        "timeout",
        "notFound",
        "fileNotFound",
        "weird_unknown",
    ],
)
async def test_transient_notfound_unknown_fallback_eligible(error):
    reader = _FakeReader(resp={"ok": False, "error": error})
    with pytest.raises(SourceFetchError) as ei:
        await _load(_src(reader))
    assert ei.value.fallback_eligible is True


@pytest.mark.asyncio
async def test_unconfigured_singleton_non_fallback_policy():
    with pytest.raises(SourcePolicyError) as ei:
        await _load(drive_file_source)
    assert ei.value.payload_code == "drive_reader_unconfigured"
    assert ei.value.fallback_eligible is False


@pytest.mark.asyncio
async def test_transport_exception_fetch():
    reader = _FakeReader(raises=RuntimeError("socket reset"))
    with pytest.raises(SourceFetchError) as ei:
        await _load(_src(reader))
    assert ei.value.payload_code == "drive_transport_error"
    assert ei.value.fallback_eligible is True


# ===========================================================================
# Malformed → non-fallback SourceParseError + strict ok
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resp",
    [
        "not-a-mapping",
        {"ok": True, "content": "x"},  # missing kind
        {"ok": True, "kind": "pdf", "content": "x"},  # unknown kind
        {"ok": True, "kind": "text"},  # missing content
        {"ok": True, "kind": "text", "content": 123},  # type mismatch
        {"ok": True, "kind": "binary", "content": "not-bytes"},
    ],
)
async def test_malformed_response_parse_error(resp):
    reader = _FakeReader(resp=resp)
    with pytest.raises(SourceParseError) as ei:
        await _load(_src(reader))
    assert ei.value.fallback_eligible is False


@pytest.mark.asyncio
async def test_strict_ok_truthy_non_bool_is_not_ok():
    """`ok: 1` (truthy non-bool) must NOT be treated as
    success — strict identity (phase-9 parity). With no
    error string it falls to the conservative fetch
    branch."""
    reader = _FakeReader(resp={"ok": 1, "kind": "text",
                               "content": "x"})
    with pytest.raises(SourceFetchError):
        await _load(_src(reader))


@pytest.mark.asyncio
async def test_ok_false_no_error_string_is_fetch():
    reader = _FakeReader(resp={"ok": False})
    with pytest.raises(SourceFetchError):
        await _load(_src(reader))


# ===========================================================================
# Args
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"file_id": "F1"},  # no source_id
        {"source_id": "in1"},  # no file_id
        {"source_id": "", "file_id": "F1"},
        {"source_id": "in1", "file_id": ""},
    ],
)
async def test_bad_args_parse_error(args):
    reader = _FakeReader(resp={"ok": True, "kind": "text",
                               "content": "x"})
    with pytest.raises(SourceParseError) as ei:
        await _src(reader).load(
            args=args, as_of_datetime=None, clock=_clock
        )
    assert ei.value.payload_code == "source_drive_file_args_invalid"


# ===========================================================================
# Cred path = Protocol DI (no google SDK in loader path; no
# new credential mechanism — same as registry_cache)
# ===========================================================================


def test_no_google_sdk_import_at_module_load():
    """The OAuth/cred path is reused via Protocol DI — the
    loader module must NOT import googleapiclient / google
    at module load (the concrete client + its creds are
    injected from OUTSIDE, exactly like the registry
    cache's GoogleDriveClient Protocol)."""
    src = inspect.getsource(drive_mod)
    tree = ast.parse(src)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "googleapiclient" not in imported
    assert "google" not in imported
    assert "oauth2client" not in imported


def test_reader_protocol_runtime_checkable():
    assert isinstance(_FakeReader(), DriveFileReader)

    class _Bad:
        pass

    assert not isinstance(_Bad(), DriveFileReader)


def test_loader_satisfies_source_loader_protocol():
    assert isinstance(drive_file_source, SourceLoader)
    assert isinstance(_src(_FakeReader()), SourceLoader)


def test_descriptor_uses_oauth_read_only_versioned():
    d = drive_file_source.descriptor
    assert isinstance(d, SourceDescriptor)
    assert d.id == SOURCE_DRIVE_FILE_ID
    assert ToolCapabilityTag.READ_EXTERNAL in d.tags
    assert ToolCapabilityTag.USES_OAUTH in d.tags
    assert not (
        d.tags
        & {
            ToolCapabilityTag.WRITE_EXTERNAL,
            ToolCapabilityTag.SEND_MESSAGE,
            ToolCapabilityTag.FILESYSTEM_WRITE,
        }
    )
    assert d.supports_versioning is True
    assert d.supported_selection_methods == [SelectionMethod.STABLE_ID]


def test_register_into_fresh_registries():
    sources = SourceRegistry()
    loaders = SourceLoaderRegistry()
    register_drive_file(sources=sources, loaders=loaders)
    assert sources.lookup(SOURCE_DRIVE_FILE_ID) is (
        drive_file_source.descriptor
    )
    assert loaders.require(SOURCE_DRIVE_FILE_ID) is drive_file_source
    with pytest.raises(DuplicateDescriptorError):
        register_drive_file(sources=sources, loaders=loaders)


def test_builtin_registered_on_import():
    import app.v2.sources as src

    assert SOURCE_DRIVE_FILE_ID in src.SOURCE_LOADERS
    assert src.SOURCES.lookup(SOURCE_DRIVE_FILE_ID) is not None
