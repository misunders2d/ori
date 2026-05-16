"""v2 scheduler — ``source_local_file`` loader + §5.3.1 fence.

Phase 10 slice 3 per ``docs/PHASE_10_PLAN.md`` §3.2 +
``docs/CONTRACTS_V2_DESIGN.md`` §5.3.1.

The HIGHEST-RISK source type — it reads the bot's own
filesystem. The fence is **separator-aware** and
**symlink-resolving**:

    resolved = Path(p).resolve(strict=True)   # canonicalises
    root     = Path(allowed_root).resolve(strict=True)
    if not resolved.is_relative_to(root): refuse
    if _hits_deny_list(resolved):          refuse

NEVER ``os.path.realpath(...) + startswith(root)`` — a
string prefix admits the sibling-prefix bypass
(``/safe/root_evil/x`` against allowed ``/safe/root``).
``is_relative_to`` compares path COMPONENTS so the sibling
is rejected; ``resolve(strict=True)`` follows every symlink
component BEFORE the check so a symlink whose target
escapes the root (or points into the deny-list) is caught.

Every fence rejection — outside-root, ``..`` traversal,
absolute-escape, symlink-escape, deny-list hit (even
WITHIN an allowed root) — raises the NON-fallback
:class:`SourceSecurityError`. Oversize / non-allowlisted
mime raise the NON-fallback :class:`SourcePolicyError`.
Per §3.5 neither is ``fallback_eligible`` — a denied read
can NEVER serve a stale cached snapshot. Only a genuinely
absent (but contained) file is a fallback-eligible
:class:`SourceFetchError` (design §5.3.2 lists "source
missing" under ``fallback_policy``); a missing-target
symlink escape is classified SECURITY first, never
downgraded to the fallback path.

TOCTOU defense (codex slice-3 🔴): after the fence the
file is opened ONCE with ``O_NOFOLLOW | O_CLOEXEC`` and
read THROUGH that fd via ``fstat`` — the path is never
re-opened / re-resolved, so the check and the read hit the
SAME inode. A post-fence swap of the final component to a
symlink makes ``os.open`` fail (``ELOOP``) →
``SourceSecurityError``. The size limit is enforced on the
fd read (cap ``max_bytes + 1``), never on a prior
``stat()`` (which is itself TOCTOU — the file can grow
between stat and read).

``allowed_roots`` defaults to EMPTY (deny-all) — admin
opt-in per root lands when the loader is wired (later
phase). ``max_bytes`` default 1 MiB; the full §3.4
``on_oversize`` branching (store_pointer_only / redact /
hash_only) is the slice-4 snapshot writer's job — the
loader enforces the hard ceiling as ``fail_and_alert``
(``SourcePolicyError``).

Arg contract — ``args``:
- ``source_id`` (str, required) — rides in args (slice-1
  ``SourceLoader`` protocol is frozen; the slice-8
  resolver injects it).
- ``path`` (str, required) — the file to read.
- ``allow_binary`` (bool, optional, default False) — opt
  in to non-text mime (design §5.3.1: binary blocked
  unless explicitly opted in).
"""

from __future__ import annotations

import base64
import errno
import fnmatch
import json
import os
import stat as _stat
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from app.v2.descriptors.source import SourceDescriptor
from app.v2.enums import SelectionMethod
from app.v2.registry import SOURCES, SourceRegistry
from app.v2.sources.contract import (
    SourceResult,
    canonical_bytes,
    content_hash_for,
)
from app.v2.sources.errors import (
    SourceFetchError,
    SourceParseError,
    SourcePolicyError,
    SourceSecurityError,
)
from app.v2.sources.registry import (
    SOURCE_LOADERS,
    SourceLoaderRegistry,
    register_source_loader,
)
from app.v2.tool_tags import ToolCapabilityTag


SOURCE_LOCAL_FILE_ID = "source_local_file"
_DEFAULT_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB

# Repo root = the dir containing ``app/`` —
# app/v2/sources/local_file.py → parents[3].
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Deny-list (design §5.3.1) — blocked even WITHIN an
# allowed root. Directory prefixes + filename globs,
# resolved against the repo root.
_DENY_DIR_RELS = (
    "data/contract_state",
    "data/contract_audit",
    "data/contracts",
)
_DENY_FILE_RELS = ("data/ori-scheduler.db",)
# Filename globs matched on any path component / leaf.
_DENY_NAME_GLOBS = ("vault*", ".env*", "*.key", "*.pem", "id_rsa*")

# extension → §3.6 canonical kind
_EXT_KIND = {
    ".txt": "text",
    ".text": "text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}


LOCAL_FILE_DESCRIPTOR = SourceDescriptor(
    id=SOURCE_LOCAL_FILE_ID,
    description=(
        "Read a text/markdown/json/yaml file under an "
        "admin-allowlisted root, behind the §5.3.1 "
        "separator-aware symlink-resolving path fence."
    ),
    tags={ToolCapabilityTag.READ_EXTERNAL},
    supports_versioning=False,
    supported_selection_methods=[SelectionMethod.CONTENT_HASH],
)


def _read_capped(fd: int, limit: int) -> bytes:
    """Read at most ``limit`` bytes from ``fd`` (handles
    short reads). Caller passes ``max_bytes + 1`` and
    rejects when the result exceeds ``max_bytes`` — so an
    oversize/grown file is never fully read into memory."""
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        block = os.read(fd, remaining)
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def _resolve_deny_dirs() -> tuple[Path, ...]:
    out: list[Path] = []
    for rel in (*_DENY_DIR_RELS, *_DENY_FILE_RELS):
        out.append((_REPO_ROOT / rel).resolve(strict=False))
    return tuple(out)


class LocalFileSource:
    """:class:`app.v2.sources.contract.SourceLoader` impl
    for allowlisted local files behind the §5.3.1 fence.

    ``allowed_roots`` empty ⇒ deny-all (safe default).
    """

    descriptor: SourceDescriptor = LOCAL_FILE_DESCRIPTOR

    def __init__(
        self,
        *,
        allowed_roots: Iterable[str | Path] = (),
        max_bytes: int = _DEFAULT_MAX_BYTES,
        deny_dirs: Optional[Iterable[str | Path]] = None,
        deny_name_globs: Iterable[str] = _DENY_NAME_GLOBS,
    ) -> None:
        # Roots resolved once (strict=False — a configured
        # root that does not yet exist is still a valid
        # containment anchor; the FILE check is strict).
        self._roots: tuple[Path, ...] = tuple(
            Path(r).resolve(strict=False) for r in allowed_roots
        )
        self._max_bytes = max_bytes
        self._deny_dirs: tuple[Path, ...] = (
            tuple(
                Path(d).resolve(strict=False) for d in deny_dirs
            )
            if deny_dirs is not None
            else _resolve_deny_dirs()
        )
        self._deny_globs = tuple(deny_name_globs)

    # ---- fence ----------------------------------------------------------

    def _within_an_allowed_root(self, resolved: Path) -> bool:
        return any(
            resolved == root or resolved.is_relative_to(root)
            for root in self._roots
        )

    def _hits_deny_list(self, resolved: Path) -> bool:
        for d in self._deny_dirs:
            if resolved == d or resolved.is_relative_to(d):
                return True
        # filename globs matched on EVERY path component, so
        # a denied dir/file name anywhere in the resolved
        # path is caught (e.g. ``vault`` segment, ``.env``).
        for part in resolved.parts:
            if any(
                fnmatch.fnmatch(part, g) for g in self._deny_globs
            ):
                return True
        return False

    def _enforce_fence(self, resolved: Path) -> None:
        """Raise :class:`SourceSecurityError` (non-fallback)
        unless ``resolved`` is inside an allowed root AND
        not in the deny-list. Empty allowlist ⇒ everything
        refused."""
        if not self._within_an_allowed_root(resolved):
            raise SourceSecurityError(
                f"path {str(resolved)!r} is outside every "
                "allowed root",
                code="path_outside_allowed_root",
            )
        if self._hits_deny_list(resolved):
            raise SourceSecurityError(
                f"path {str(resolved)!r} hits the deny-list "
                "(blocked even within an allowed root)",
                code="path_in_deny_list",
            )

    # ---- load -----------------------------------------------------------

    async def load(
        self,
        *,
        args: dict[str, Any],
        as_of_datetime: Optional[datetime],  # noqa: ARG002 - local file has no as-of dimension
        clock: Callable[[], datetime],
    ) -> SourceResult:
        source_id = self._req_str(args, "source_id")
        raw_path = self._req_str(args, "path")
        allow_binary = bool(args.get("allow_binary", False))

        candidate = Path(raw_path)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            # SECURITY classification wins over not-found: a
            # missing-target symlink escape / deny-list path
            # must NOT be downgraded to the fallback-eligible
            # not_found path. Run the fence on the best-effort
            # canonical form first.
            best_effort = candidate.resolve(strict=False)
            self._enforce_fence(best_effort)
            # Contained + truly absent → fallback-eligible
            # (design §5.3.2 lists "source missing").
            raise SourceFetchError(
                f"local file {str(best_effort)!r} not found",
                code="source_local_file_not_found",
            )
        except (OSError, RuntimeError) as exc:
            # Symlink loop / permission on resolve, etc.
            raise SourceSecurityError(
                f"cannot canonicalise {raw_path!r}: {exc}",
                code="path_canonicalisation_failed",
            ) from exc

        self._enforce_fence(resolved)

        kind = _EXT_KIND.get(resolved.suffix.lower())
        if kind is None:
            if not allow_binary:
                raise SourcePolicyError(
                    f"extension {resolved.suffix!r} is not "
                    "in the mime allowlist "
                    "(text/markdown/json/yaml); pass "
                    "allow_binary=True to opt in",
                    code="mime_not_allowlisted",
                )
            kind = "binary"

        # TOCTOU defense (codex slice-3 🔴): open the
        # resolved path ONCE with O_NOFOLLOW | O_CLOEXEC,
        # then fstat + read THROUGH this fd. The fence check
        # and the read operate on the SAME inode — the path
        # is NEVER re-opened or re-resolved after the check.
        # A post-fence swap of the final component to a
        # symlink makes os.open fail (O_NOFOLLOW → ELOOP) →
        # non-fallback SourceSecurityError; content is never
        # served, no cache fallback.
        try:
            fd = os.open(
                resolved,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SourceSecurityError(
                    f"path {str(resolved)!r} became a "
                    "symlink after the fence check "
                    "(O_NOFOLLOW)",
                    code="symlink_swapped_after_fence",
                ) from exc
            if exc.errno == errno.ENOENT:
                # Contained path vanished in the race →
                # genuinely absent (design §5.3.2 lists
                # "source missing" under fallback_policy).
                raise SourceFetchError(
                    f"local file {str(resolved)!r} not "
                    "found",
                    code="source_local_file_not_found",
                ) from exc
            raise SourceSecurityError(
                f"cannot open {str(resolved)!r} no-follow: "
                f"{exc}",
                code="path_open_failed",
            ) from exc
        try:
            st = os.fstat(fd)
            if not _stat.S_ISREG(st.st_mode):
                raise SourceSecurityError(
                    f"path {str(resolved)!r} is not a "
                    "regular file",
                    code="path_not_a_regular_file",
                )
            # Read at most max_bytes+1 THROUGH the fd. Never
            # trust a prior stat() size — the file may grow
            # between stat and read (TOCTOU); cap the read
            # itself so a giant file is never slurped.
            raw = _read_capped(fd, self._max_bytes + 1)
        finally:
            os.close(fd)

        if len(raw) > self._max_bytes:
            raise SourcePolicyError(
                f"local file exceeds max {self._max_bytes} "
                "bytes (on_oversize: fail_and_alert; "
                "slice-4 writer wires the other branches)",
                code="source_local_file_oversize",
            )

        return self._build_result(
            kind=kind,
            raw=raw,
            source_id=source_id,
            fetched_at=clock(),
        )

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _req_str(args: dict[str, Any], name: str) -> str:
        if name not in args:
            raise SourceParseError(
                f"source_local_file requires args['{name}']",
                code="source_local_file_args_invalid",
            )
        v = args[name]
        if not isinstance(v, str) or not v:
            raise SourceParseError(
                f"source_local_file args['{name}'] must be "
                "a non-empty str",
                code="source_local_file_args_invalid",
            )
        return v

    def _build_result(
        self,
        *,
        kind: str,
        raw: bytes,
        source_id: str,
        fetched_at: datetime,
    ) -> SourceResult:
        if kind in ("text", "markdown", "yaml"):
            try:
                content: Any = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SourceParseError(
                    f"{kind} file is not valid utf-8: {exc}",
                    code="source_local_file_decode_failed",
                ) from exc
            content_bytes = canonical_bytes(kind, raw)  # verbatim
        elif kind == "json":
            try:
                content = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SourceParseError(
                    f"json file is not parseable: {exc}",
                    code="source_local_file_json_invalid",
                ) from exc
            content_bytes = canonical_bytes("json", content)
        else:  # binary (opt-in)
            content = {"_b64": base64.b64encode(raw).decode("ascii")}
            content_bytes = canonical_bytes("binary", raw)

        return SourceResult(
            content=content,
            content_bytes=content_bytes,
            source_kind=SOURCE_LOCAL_FILE_ID,
            source_id=source_id,
            fetched_at=fetched_at,
            content_hash=content_hash_for(content_bytes),
            item_count=1,
            source_version=None,
            selection_method=SelectionMethod.CONTENT_HASH,
        )


#: Production singleton — EMPTY allowed_roots (deny-all
#: until an admin opts a root in when the loader is wired).
local_file_source = LocalFileSource()


def register_local_file(
    *,
    sources: SourceRegistry = SOURCES,
    loaders: SourceLoaderRegistry = SOURCE_LOADERS,
) -> None:
    """Register ``source_local_file`` (atomic paired
    registration). Default args target the production
    singletons; tests pass fresh registries."""
    register_source_loader(
        local_file_source, sources=sources, loaders=loaders
    )


__all__ = [
    "SOURCE_LOCAL_FILE_ID",
    "LOCAL_FILE_DESCRIPTOR",
    "LocalFileSource",
    "local_file_source",
    "register_local_file",
]
