# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build context on disk: creating one, reading it back, verifying it.

The other half of :mod:`mcuhome.context`. That module is the context
*format* — the manifest as data, and the normative ID rule every party
computes the same value with. This one is everything that touches a
filesystem: walking a directory and hashing what is in it, passing
patches through, writing and reading ``manifest.yaml``, and the
server-side integrity check.

ADR 0020 puts the two in different packages, and the ID rule is the
reason. The build server recomputes a context ID from bytes it received
(ADR 0019 §8) and must not carry build logic to do it; a workbench
creates contexts and needs all of this. Splitting here is what lets the
first depend on the second's vocabulary without its machinery — and, as
a side effect, keeps a YAML parser out of the package whose only job is
to be identical everywhere.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ruamel.yaml import YAML, YAMLError

from mcuhome.context import (
    BACKEND_DIR,
    CONTEXT_VERSION,
    MANIFEST_FILE,
    MODEL_FILE,
    PATCHES_DIR,
    ContainerPin,
    ContextFile,
    ContextManifest,
    SdkPin,
    context_id,
    validate_manifest,
)
from mcuhome.errors import BuildError
from mcuhome.model import DeviceModel

__all__ = [
    "ContextFormatVersionError",
    "ContextVerification",
    "FileMismatch",
    "create_context",
    "read_context_manifest",
    "verify_context",
    "write_context_manifest",
]

_LAYER_NAME = re.compile(r"[a-z][a-z0-9_-]*\Z")
_PATCH_NAME = re.compile(r"[0-9]{4}-[A-Za-z0-9._-]+\.patch\Z")

#: Read in blocks rather than whole — same reasoning as in
#: :mod:`mcuhome.manifest`: a context can carry large patches.
_HASH_BLOCK = 1 << 20


class ContextFormatVersionError(BuildError):
    """The manifest states a ``context`` format version nothing here implements.

    A type of its own, and for this one refusal only, because the build
    container contract answers it differently from every other manifest
    this package cannot read: "A program MUST check the ``context`` format
    version and, for a version it does not implement, fail the invocation
    with ``status: "unsupported"``, ``reason: "unsupported.context"`` …
    and the version it found in ``error.details``"
    (build-container-contract.md §3.2). ``unsupported`` and not
    ``failure``, "because the program is refusing a document written to a
    specification it does not have, which a backend can act on by choosing
    another image — nothing about this context is broken".

    A caller that cannot tell this refusal from a truncated one cannot
    make that distinction, and would have to either re-parse the manifest
    to recover the number or match on an error message. :attr:`found` is
    what it reports instead. Rendered it is an ordinary
    :class:`~mcuhome.errors.BuildError`, so a caller that does not care
    keeps catching what it caught before.
    """

    def __init__(self, message: str, *, hint: str, found: object) -> None:
        super().__init__(message, hint=hint)
        #: What the manifest's ``context`` key carried, verbatim and
        #: unvalidated — ``None`` when it carried nothing at all, which is
        #: a manifest that names no format version rather than one that
        #: names an unknown version.
        self.found = found


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
    directory are not content (build-container-contract.md §3.2), so
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
        raise ContextFormatVersionError(
            f"The context manifest {path} has format version {found!r}, and this "
            f"builder implements version {CONTEXT_VERSION}.",
            hint=(
                "the context format is a versioned contract: a mismatch is a refusal "
                "that names both numbers, never a guess. Recreate the context with a "
                "matching mcuhome version."
            ),
            found=found,
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
    validate_manifest(manifest)
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
