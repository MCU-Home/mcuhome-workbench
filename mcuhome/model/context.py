# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build context — the self-contained input artifact of a remote build.

A build context is a plain directory: ``manifest.yaml``, the canonical
device model under ``model/``, optionally patches under
``patches/<layer>/``. It contains everything a build needs except the
toolchain and Zephyr (in the digest-pinned builder container) and the SDK
(fetched as a hash-pinned package), so the context plus the pinned
container digest reproduces the build years later. It travels as an
archive, but the archive is transport: the directory is the artifact.

**The context ID is normative — fixed with ``context`` format version 1,
and it can never change afterwards.** Everything that ever names a
context — integrity verification, artifact attribution ("built from
*this*"), archival references — depends on computing the same ID from
the same inputs forever. The rule is stated in
``docs/design/build-container-contract.md`` §3.3 (ADR 0018); this
module implements it. The ID is the SHA-256 over the canonical JSON
(RFC 8785) of exactly this document::

    {"container": {"digest": ...},
     "files": [{"path": ..., "sha256": ...}, ...],
     "sdk": {"sha256": ...},
     "target": {"board": ...}}

— the manifest's build-relevant fields under the contract's fixed
names (``sdk.sha256`` carries the manifest's
``mcuhome.package.sha256``). ``files`` is sorted by ``path`` in
ascending byte order of its UTF-8 encoding — which UTF-8 makes equal
to code-point order, so a plain string sort implements it — and every
listed file contributes its own content hash; the sort only makes the
encoding deterministic. Explicitly excluded, so they can never
influence the ID: ``created`` (informational), ``mcuhome.constraint``
(the intent, not the resolution), ``mcuhome.version`` and
``mcuhome.package.url`` (names for the bytes ``package.sha256`` already
pins), and ``container.image``/``container.tag`` (the digest alone
identifies the container, so a context resolved via a moving tag and
one resolved via the equivalent versioned tag hash identically).
``manifest.yaml`` itself and the backend-written ``.mcuhome/`` runtime
directory are never integrity entries, so they cannot influence the ID
either. Neither the YAML file bytes nor the transport archive bytes are
ever hashed — neither serialization is deterministic. New
build-relevant fields enter the hash only together with a bump of the
``context`` format version.

Patches carry no manifest section of their own. A patch is a file: its
target layer is its subfolder, its application order is its ``NNNN-``
filename prefix, and its integrity entry sits in ``files`` like every
other file's. There is deliberately no declared patch list that could
disagree with the files actually present, so a build server re-derives
its patch policy from the paths alone.

**This module is the format and the rule; it never touches a
filesystem.** Creating a context directory, hashing what is in one and
:func:`~mcuhome.workbench.contextdir.verify_context` — the server-side integrity
primitive that recomputes every file hash and the ID from the bytes
actually present — are :mod:`mcuhome.workbench.contextdir`. The cut is ADR 0020's:
the build server recomputes a context ID from bytes it received off a
socket and must carry no build logic to do it, so :func:`context_id`
takes values and not a directory, and this module imports nothing but
the standard library and :mod:`mcuhome.model.errors`.

:data:`CONTEXT_ID_VECTORS` is the conformance suite that keeps a second
implementation honest — the frozen rule stated as inputs and outputs
rather than as code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from mcuhome.model.errors import BuildError

__all__ = [
    "BACKEND_DIR",
    "CONTEXT_ID_VECTORS",
    "CONTEXT_VERSION",
    "MANIFEST_FILE",
    "MODEL_FILE",
    "PATCHES_DIR",
    "ContainerPin",
    "ContextFile",
    "ContextManifest",
    "SdkPin",
    "canonical_json",
    "context_id",
    "validate_manifest",
    "vector_id",
]

#: Format version of the context manifest. The normative hashing rule is
#: locked to it: a field can join the hashed document only together with
#: a bump here, and version 1's rule never changes.
CONTEXT_VERSION = 1

#: The one file a builder must parse first, at the top of the context.
MANIFEST_FILE = "manifest.yaml"

#: Where the canonical device model lives inside the context — the
#: existing wire format (:mod:`mcuhome.model.model`), unchanged.
MODEL_FILE = "model/device-model.json"

#: Where patches live: ``patches/<layer>/NNNN-name.patch``.
PATCHES_DIR = "patches"

#: The backend-written runtime directory inside a mounted context
#: (``.mcuhome/command.json``), as contract v1 was first drafted.
#: Plumbing, not content: never an integrity entry, never identity.
#:
#: **The contract has moved on and this constant has not yet.** The
#: per-invocation request document now lives in a backend-owned
#: directory outside the context, and there is no ``.mcuhome/`` in a
#: context at all (build-container-contract.md §3.1, §5.2) — which is
#: what makes the context a genuinely read-only mount. Removing the
#: directory from the exclusion rules is migration work; keeping it
#: excluded in the meantime is harmless, because a context that never
#: contains one cannot be affected by the exclusion.
BACKEND_DIR = ".mcuhome"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


# --------------------------------------------------------------------------
# The manifest, as data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextFile:
    """One integrity entry: a context-relative path and its content hash."""

    #: Relative to the context directory, forward slashes on every
    #: platform. Part of the hashed identity, so its spelling is checked
    #: strictly (:func:`context_id` refuses anything else).
    path: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class SdkPin:
    """The resolved mcuhome pin — the manifest's ``mcuhome:`` section.

    ``constraint`` is the original intent from the device configuration
    (``^2.3.6``); ``version`` and the package are what it resolved to at
    context creation. Only ``sha256`` is part of the context's identity:
    the constraint is intent, and the version and URL are names for the
    bytes the hash already pins.
    """

    constraint: str
    version: str
    #: Where the resolved SDK package can be fetched. A hint only — a
    #: server resolves (version, sha256) against its own source list.
    url: str
    sha256: str


@dataclass(frozen=True)
class ContainerPin:
    """The resolved builder container — the manifest's ``container:`` section.

    Only ``digest`` identifies the container; image and tag are the
    human-readable trail of how it was resolved.
    """

    image: str
    tag: str
    digest: str


@dataclass(frozen=True)
class ContextManifest:
    """``manifest.yaml``, as an object."""

    #: Informational — never hashed. ISO 8601 UTC, seconds precision.
    created: str
    sdk: SdkPin
    container: ContainerPin
    #: The target board — the manifest's ``target:`` section.
    board: str
    #: The integrity list: every file in the context except the manifest
    #: itself, patches included, sorted by path.
    files: tuple[ContextFile, ...]
    #: ``sha256:<hex>`` — the declared context ID. Advisory like every
    #: declared value; :func:`verify_context` recomputes it.
    id: str
    context_version: int = CONTEXT_VERSION

    def compute_id(self) -> str:
        """The ID this manifest's hashed fields yield, per the normative rule."""
        return context_id(
            container_digest=self.container.digest,
            sdk_sha256=self.sdk.sha256,
            board=self.board,
            files=self.files,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context_version,
            "created": self.created,
            "mcuhome": {
                "constraint": self.sdk.constraint,
                "version": self.sdk.version,
                "package": {"url": self.sdk.url, "sha256": self.sdk.sha256},
            },
            "container": {
                "image": self.container.image,
                "tag": self.container.tag,
                "digest": self.container.digest,
            },
            "target": {"board": self.board},
            "files": [entry.to_dict() for entry in self.files],
            "id": self.id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ContextManifest:
        mcuhome = data["mcuhome"]
        package = mcuhome["package"]
        container = data["container"]
        return ContextManifest(
            context_version=int(data["context"]),
            created=str(data["created"]),
            sdk=SdkPin(
                constraint=str(mcuhome["constraint"]),
                version=str(mcuhome["version"]),
                url=str(package["url"]),
                # Hashes and paths are deliberately not coerced: they are
                # identity, and _validate_manifest type-checks them.
                sha256=package["sha256"],
            ),
            container=ContainerPin(
                image=str(container["image"]),
                tag=str(container["tag"]),
                digest=container["digest"],
            ),
            board=data["target"]["board"],
            files=tuple(
                ContextFile(path=item["path"], sha256=item["sha256"]) for item in data["files"]
            ),
            id=data["id"],
        )


# --------------------------------------------------------------------------
# The normative hash
# --------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """*value* as RFC 8785 canonical JSON — for this module's value domain.

    The hashed document contains only objects with fixed ASCII key
    names, arrays, and string values. For that domain Python's own
    serializer *is* the JSON Canonicalization Scheme: keys sorted
    (code-point order, which equals JCS's UTF-16 order for ASCII keys),
    minimal separators, strings escaped the ECMAScript way — the
    two-character escapes, lowercase ``\\u00xx`` for the remaining
    control characters, everything else emitted literally — and the
    UTF-8 encoding done by the caller. Deliberately this small instead
    of a full JCS implementation: JCS's only hard part is number
    serialization, and numbers never occur in the document.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require_sha256(value: object, *, what: str) -> None:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise BuildError(
            f"{what} is not a SHA-256 hash: {value!r}.",
            hint=(
                "the canonical spelling is exactly 64 lowercase hex digits — one "
                "spelling per hash, so two manifests can never name the same bytes "
                "differently"
            ),
        )


def _require_digest(value: object, *, what: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise BuildError(
            f"{what} is not a sha256 digest: {value!r}.",
            hint='the canonical form is "sha256:" followed by 64 lowercase hex digits',
        )


def _require_board(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BuildError(
            "The context names no target board.",
            hint=(
                "the board is a hashed identity field — a context that does not say "
                "what it builds for has no identity"
            ),
        )


def _require_path(value: object) -> None:
    usable = (
        isinstance(value, str)
        and value
        and "\\" not in value
        and not value.startswith("/")
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )
    if not usable:
        raise BuildError(
            f"{value!r} is not a usable context path.",
            hint=(
                "context paths are relative, use forward slashes and contain no "
                '"." or ".." segments — they are hashed identity and extraction '
                "targets at once"
            ),
        )


def _require_files(entries: Iterable[ContextFile]) -> None:
    seen: set[str] = set()
    for entry in entries:
        _require_path(entry.path)
        _require_sha256(entry.sha256, what=f'The hash of "{entry.path}"')
        if entry.path == MANIFEST_FILE or entry.path.split("/", 1)[0] == BACKEND_DIR:
            raise BuildError(
                f'The integrity list must not name "{entry.path}".',
                hint=(
                    "manifest.yaml describes the list and .mcuhome/ is backend "
                    "plumbing — the contract keeps both out of the context's "
                    "identity (build-container-contract.md §3.2)"
                ),
            )
        if entry.path in seen:
            raise BuildError(
                f'The integrity list names "{entry.path}" twice.',
                hint="one entry per file — a duplicate would make the canonical encoding ambiguous",
            )
        seen.add(entry.path)


def context_id(
    *,
    container_digest: str,
    sdk_sha256: str,
    board: str,
    files: Iterable[ContextFile],
) -> str:
    """The context ID — SHA-256 over the canonical form of the hashed fields.

    This function is the normative rule of the module docstring, locked
    with ``context`` format version 1. It takes exactly the four hashed
    inputs and nothing else, so an informational field *cannot* leak
    into the ID by construction. Inputs are checked strictly rather than
    normalized: an ID computed over a mistyped digest would be silently
    wrong forever, and normalizing (say, uppercase hex) would give the
    same bytes two names.
    """
    entries = tuple(files)
    _require_digest(container_digest, what="The container digest")
    _require_sha256(sdk_sha256, what="The SDK package hash")
    _require_board(board)
    _require_files(entries)
    document = {
        "container": {"digest": container_digest},
        "files": [entry.to_dict() for entry in sorted(entries, key=lambda entry: entry.path)],
        "sdk": {"sha256": sdk_sha256},
        "target": {"board": board},
    }
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_manifest(manifest: ContextManifest) -> None:
    """Check a parsed manifest's shape and spelling, not its truth.

    Every hashed field, spelled the one way the format allows, plus the
    declared ID. Whether the declared values match bytes on a disk is
    :func:`~mcuhome.workbench.contextdir.verify_context`'s question and needs a
    disk to answer.
    """
    _require_digest(manifest.container.digest, what="The container digest")
    _require_sha256(manifest.sdk.sha256, what="The SDK package hash")
    _require_board(manifest.board)
    _require_files(manifest.files)
    _require_digest(manifest.id, what="The context id")


# --------------------------------------------------------------------------
# Conformance vectors
# --------------------------------------------------------------------------

#: The frozen rule, stated as inputs and outputs.
#:
#: ADR 0019 §8 obliges the build server to recompute the context ID from
#: the bytes it received, and ADR 0018 §6 says both sides of the contract
#: compute the same value. Whoever writes the second implementation — a
#: third-party build container, a server in another language, a future
#: rewrite of this one — needs something to be wrong against that is not
#: this file's source code. These are it: run :func:`context_id` (or your
#: own) over each ``inputs`` and you must get ``id``.
#:
#: **Version 1's vectors never change.** A vector may be added; an
#: existing one may not be altered, because altering one would mean the
#: rule changed, and the rule changing means a new ``context`` format
#: version (:data:`CONTEXT_VERSION`) with vectors of its own. They
#: cover what an independent implementation gets wrong: an empty
#: file list (the document still has the key), the ordinary single-file
#: case, a list whose given order is not its hashed order, and a path
#: with non-ASCII characters — which :func:`canonical_json` emits
#: literally rather than as ``\\u`` escapes, and which therefore only
#: hashes the same if the other side agrees about that.
#:
#: The board names below are hash inputs and nothing else: the rule never
#: looks a board up, so a vector stays valid after the registry drops the
#: board it happens to name. Renaming one here would change an ID.
CONTEXT_ID_VECTORS: tuple[dict[str, Any], ...] = (
    {
        "name": "no files",
        "inputs": {
            "container_digest": "sha256:" + "1" * 64,
            "sdk_sha256": "a" * 64,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (),
        },
        "id": "sha256:6e6eea996091e2768992b64261e87422ada9592d509592d2c6bbac33e6d18c2d",
    },
    {
        "name": "one file",
        "inputs": {
            "container_digest": "sha256:" + "0" * 64,
            "sdk_sha256": "b" * 64,
            "board": "nrf52840dk/nrf52840",
            "files": (("model/device-model.json", "c" * 64),),
        },
        "id": "sha256:1a7a7781357a66b3f7c317f9b62a64f41968e27b81a4b15eb086feba097e84e5",
    },
    {
        "name": "given out of order",
        "inputs": {
            "container_digest": "sha256:" + "ab" * 32,
            "sdk_sha256": "d" * 64,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (
                ("patches/zephyr/0002-b.patch", "2" * 64),
                ("model/device-model.json", "1" * 64),
                ("patches/zephyr/0001-a.patch", "3" * 64),
                ("keys/signing.pub", "4" * 64),
            ),
        },
        "id": "sha256:c92c21842c6559ee15625ecded528f8c2469e00369bb73f9071a246a74f83ee2",
    },
    {
        # The vector tests_py/test_context.py has pinned since the format
        # was written, kept where a second implementation can reach it.
        "name": "model and one patch",
        "inputs": {
            "container_digest": "sha256:" + "ab" * 32,
            "sdk_sha256": "cd" * 32,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (
                ("model/device-model.json", "11" * 32),
                ("patches/zephyr/0001-fix.patch", "22" * 32),
            ),
        },
        "id": "sha256:dde9df3b7ab59f8ad8197b6916f437ed3502ce88275b48f5e122b89e48b99c3f",
    },
    {
        "name": "non-ascii path",
        "inputs": {
            "container_digest": "sha256:" + "ef" * 32,
            "sdk_sha256": "e" * 64,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (("model/dévice-modèl.json", "5" * 64),),
        },
        "id": "sha256:fb356f312ca5ff43acbb0098204494638a4b2f412f4a4b141c66676635861846",
    },
)


def vector_id(vector: dict[str, Any]) -> str:
    """Run one :data:`CONTEXT_ID_VECTORS` entry through :func:`context_id`.

    A vector states its files as ``(path, sha256)`` pairs rather than as
    :class:`ContextFile` objects, so that the data stays copyable into a
    document another implementation can read.
    """
    inputs = vector["inputs"]
    return context_id(
        container_digest=inputs["container_digest"],
        sdk_sha256=inputs["sdk_sha256"],
        board=inputs["board"],
        files=[ContextFile(path=path, sha256=sha256) for path, sha256 in inputs["files"]],
    )
