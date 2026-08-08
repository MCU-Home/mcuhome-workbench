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
``docs/design/builder-container-contract.md`` §3.3 (ADR 0018); this
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

:func:`verify_context` is the server-side integrity primitive: it
recomputes every file hash and the context ID from the bytes actually
present and reports every disagreement. Declared values are advisory —
which is the whole reason spoofing a hash or an ID in the manifest
achieves nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError

from mcuhome.errors import BuildError
from mcuhome.model import DeviceModel

__all__ = [
    "BACKEND_DIR",
    "CONTEXT_VERSION",
    "MANIFEST_FILE",
    "MODEL_FILE",
    "PATCHES_DIR",
    "ContainerPin",
    "ContextFile",
    "ContextManifest",
    "ContextVerification",
    "FileMismatch",
    "SdkPin",
    "canonical_json",
    "context_id",
    "create_context",
    "read_context_manifest",
    "verify_context",
    "write_context_manifest",
]

#: Format version of the context manifest. The normative hashing rule is
#: locked to it: a field can join the hashed document only together with
#: a bump here, and version 1's rule never changes.
CONTEXT_VERSION = 1

#: The one file a builder must parse first, at the top of the context.
MANIFEST_FILE = "manifest.yaml"

#: Where the canonical device model lives inside the context — the
#: existing wire format (:mod:`mcuhome.model`), unchanged.
MODEL_FILE = "model/device-model.json"

#: Where patches live: ``patches/<layer>/NNNN-name.patch``.
PATCHES_DIR = "patches"

#: The backend-written runtime directory inside a mounted context
#: (``.mcuhome/command.json``, builder-container-contract.md §5.2).
#: Plumbing, not content: never an integrity entry, never identity.
BACKEND_DIR = ".mcuhome"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_LAYER_NAME = re.compile(r"[a-z][a-z0-9_-]*\Z")
_PATCH_NAME = re.compile(r"[0-9]{4}-[A-Za-z0-9._-]+\.patch\Z")

#: Read in blocks rather than whole — same reasoning as in
#: :mod:`mcuhome.manifest`: a context can carry large patches.
_HASH_BLOCK = 1 << 20


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
                    "identity (builder-container-contract.md §3.2)"
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


def _validate_manifest(manifest: ContextManifest) -> None:
    _require_digest(manifest.container.digest, what="The container digest")
    _require_sha256(manifest.sdk.sha256, what="The SDK package hash")
    _require_board(manifest.board)
    _require_files(manifest.files)
    _require_digest(manifest.id, what="The context id")


# --------------------------------------------------------------------------
# Hashing what is actually there
# --------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()


def _context_files(root: Path) -> tuple[ContextFile, ...]:
    """Every regular file under *root* that is context content, hashed.

    The manifest itself and the backend-written ``.mcuhome/`` runtime
    directory are not content (builder-container-contract.md §3.2), so
    neither is listed — and by way of that, neither can influence the ID.
    """
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST_FILE or relative.split("/", 1)[0] == BACKEND_DIR:
            continue
        entries.append(ContextFile(path=relative, sha256=_sha256_file(path)))
    entries.sort(key=lambda entry: entry.path)
    return tuple(entries)


# --------------------------------------------------------------------------
# Creating a context
# --------------------------------------------------------------------------


def _copy_patches(source: Path, target: Path) -> None:
    """Pass ``<layer>/NNNN-name.patch`` files through, refusing anything else.

    Strict on purpose: layer and order carry the whole meaning of a
    patch (there is no manifest section to say it differently), so a
    file this layout cannot express is refused here rather than silently
    carried along as a file no builder will ever apply.
    """
    if not source.is_dir():
        raise BuildError(
            f"The patches directory {source} does not exist.",
            hint=(
                "pass a directory laid out as <layer>/NNNN-name.patch — for example "
                "patches/zephyr/0001-fix-uart.patch — or leave patches out"
            ),
        )
    for layer_dir in sorted(source.iterdir()):
        if not layer_dir.is_dir():
            raise BuildError(
                f"{layer_dir} is not a patch layer.",
                hint=(
                    "patches live one level down — patches/<layer>/NNNN-name.patch — "
                    "because the subfolder names the layer the patch applies to"
                ),
            )
        if _LAYER_NAME.fullmatch(layer_dir.name) is None:
            raise BuildError(
                f'"{layer_dir.name}" is not a usable patch layer name.',
                hint=(
                    "lowercase letters, digits, - and _, starting with a letter — "
                    "like zephyr, sdk or chip"
                ),
            )
        for patch in sorted(layer_dir.iterdir()):
            if not patch.is_file():
                raise BuildError(
                    f"{patch} is not a patch file.",
                    hint=(
                        "a layer folder holds patch files only — deeper nesting has "
                        "no meaning for the application order"
                    ),
                )
            if _PATCH_NAME.fullmatch(patch.name) is None:
                raise BuildError(
                    f'"{patch.name}" is not a patch file name MCUHome can order.',
                    hint=(
                        "name patches NNNN-description.patch, like 0001-fix-uart.patch "
                        "— the numeric prefix is the application order within the layer"
                    ),
                )
            destination = target / layer_dir.name / patch.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(patch, destination)


def create_context(
    model: DeviceModel,
    *,
    out_dir: Path,
    sdk: SdkPin,
    container: ContainerPin,
    patches_dir: Path | None = None,
    created: str | None = None,
) -> ContextManifest:
    """Build a context directory from a resolved device model.

    Writes the model as ``model/device-model.json``, passes *patches_dir*
    through (laid out as ``<layer>/NNNN-name.patch``), hashes every file,
    computes the context ID and writes ``manifest.yaml`` last. *sdk* and
    *container* arrive already resolved to exact pins — resolving a
    constraint to them is a separate step, deliberately not this one.

    *out_dir* has to be new or empty: the integrity list is the whole
    truth about the directory, which it cannot be for files this
    function did not put there. *created* is for reproducing a manifest
    byte for byte (tests, mirrors); it defaults to now and never
    influences the ID either way.
    """
    if out_dir.exists():
        if not out_dir.is_dir():
            raise BuildError(
                f"The context target {out_dir} is not a directory.",
                hint="point at a new or empty directory the context can be created in",
            )
        if any(out_dir.iterdir()):
            raise BuildError(
                f"The context directory {out_dir} already contains files.",
                hint=(
                    "a context is created from scratch so its integrity list covers "
                    "everything in it — point at a new or empty directory"
                ),
            )
    model_path = out_dir / MODEL_FILE
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(model.to_json(), encoding="utf-8")
    if patches_dir is not None:
        _copy_patches(patches_dir, out_dir / PATCHES_DIR)

    files = _context_files(out_dir)
    manifest = ContextManifest(
        created=(
            created if created is not None else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        sdk=sdk,
        container=container,
        board=model.device.board,
        files=files,
        id=context_id(
            container_digest=container.digest,
            sdk_sha256=sdk.sha256,
            board=model.device.board,
            files=files,
        ),
    )
    write_context_manifest(manifest, out_dir=out_dir)
    return manifest


# --------------------------------------------------------------------------
# The manifest on disk
# --------------------------------------------------------------------------


def write_context_manifest(manifest: ContextManifest, *, out_dir: Path) -> Path:
    """Write ``manifest.yaml`` into *out_dir* and return its path.

    The YAML bytes are presentation, never identity: the ID was computed
    over the canonical JSON form before this function ran, and a reader
    re-parses the values rather than hashing the file.
    """
    path = out_dir / MANIFEST_FILE
    yaml = YAML()
    yaml.default_flow_style = False
    try:
        with path.open("w", encoding="utf-8") as handle:
            yaml.dump(manifest.to_dict(), handle)
    except OSError as error:
        raise BuildError(
            f"The context manifest {path} cannot be written: {error.strerror}.",
            hint="pick a writable location for the context directory",
        ) from error
    return path


def read_context_manifest(path: Path) -> ContextManifest:
    """Load a context ``manifest.yaml``, or refuse in plain language.

    Checks shape and spelling — the format version, every hash, every
    path — but deliberately not truth: whether the declared values match
    the bytes next to the manifest is :func:`verify_context`'s job.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"MCUHome cannot read the context manifest {path}: {error.strerror}.",
            hint=(
                f"a build context carries a {MANIFEST_FILE} at its top level — "
                "point at a directory create_context wrote"
            ),
        ) from error
    try:
        data = YAML(typ="safe").load(text)
    except YAMLError as error:
        problem = str(error).splitlines()[0] if str(error) else "unreadable syntax"
        raise BuildError(
            f"The context manifest {path} is not valid YAML ({problem}).",
            hint="it is builder output — recreate the context rather than editing it",
        ) from error
    if not isinstance(data, dict):
        raise BuildError(
            f"The context manifest {path} does not describe a context.",
            hint="it is builder output — recreate the context rather than editing it",
        )

    found = data.get("context")
    if found != CONTEXT_VERSION:
        raise BuildError(
            f"The context manifest {path} has format version {found!r}, and this "
            f"builder implements version {CONTEXT_VERSION}.",
            hint=(
                "the context format is a versioned contract: a mismatch is a refusal "
                "that names both numbers, never a guess. Recreate the context with a "
                "matching mcuhome version."
            ),
        )
    try:
        manifest = ContextManifest.from_dict(data)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise BuildError(
            f"The context manifest {path} is missing something this builder needs: {error}.",
            hint=(
                f"it states context format {CONTEXT_VERSION}, so this is a truncated "
                "or hand-edited file rather than a version mismatch. Recreate the "
                "context."
            ),
        ) from error
    _validate_manifest(manifest)
    return manifest


# --------------------------------------------------------------------------
# Verification — the server-side integrity primitive
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FileMismatch:
    """One file whose declared and actual state disagree.

    Three shapes: a tampered file carries both hashes, a listed-but-
    missing file has no actual hash, a present-but-unlisted file has no
    declared one. All three break the context ID, because the ID is
    recomputed from what is actually there.
    """

    path: str
    declared_sha256: str | None
    actual_sha256: str | None

    def describe(self) -> str:
        if self.declared_sha256 is None:
            return f"{self.path}: present but not in the integrity list"
        if self.actual_sha256 is None:
            return f"{self.path}: listed in the manifest but missing"
        return (
            f"{self.path}: hashes to {self.actual_sha256}, the manifest says {self.declared_sha256}"
        )


@dataclass(frozen=True)
class ContextVerification:
    """What :func:`verify_context` found, declared versus actual."""

    root: Path
    manifest: ContextManifest
    #: The ID of the context as it actually is: recomputed from the
    #: declared pins and the file hashes measured on disk.
    actual_id: str
    mismatches: tuple[FileMismatch, ...]

    @property
    def declared_id(self) -> str:
        return self.manifest.id

    @property
    def ok(self) -> bool:
        return not self.mismatches and self.declared_id == self.actual_id

    def problems(self) -> list[str]:
        """Every disagreement as one plain sentence, file order first."""
        messages = [mismatch.describe() for mismatch in self.mismatches]
        if self.declared_id != self.actual_id:
            messages.append(
                f"context id: the bytes present hash to {self.actual_id}, "
                f"the manifest says {self.declared_id}"
            )
        return messages


def verify_context(root: Path) -> ContextVerification:
    """Recompute every hash and the context ID from the bytes present.

    The server-side primitive behind "never trust client-declared
    hashes": the manifest's values are advisory, the bytes decide. Every
    file is re-hashed, files the manifest forgot and files it invents
    are both mismatches, and :attr:`~ContextVerification.actual_id` is
    the ID of the context as received — the one an artifact built from
    it would be attributed to. Raises :class:`~mcuhome.errors.BuildError`
    only when there is nothing to verify against: no manifest, or one
    too malformed to state declared values at all.
    """
    root = Path(root)
    manifest = read_context_manifest(root / MANIFEST_FILE)
    present = _context_files(root)
    declared = {entry.path: entry.sha256 for entry in manifest.files}
    actual = {entry.path: entry.sha256 for entry in present}
    mismatches = tuple(
        FileMismatch(
            path=path,
            declared_sha256=declared.get(path),
            actual_sha256=actual.get(path),
        )
        for path in sorted(set(declared) | set(actual))
        if declared.get(path) != actual.get(path)
    )
    return ContextVerification(
        root=root,
        manifest=manifest,
        actual_id=context_id(
            container_digest=manifest.container.digest,
            sdk_sha256=manifest.sdk.sha256,
            board=manifest.board,
            files=present,
        ),
        mismatches=mismatches,
    )
