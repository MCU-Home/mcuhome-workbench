# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build context on disk: creating one, reading it back, verifying it.

The other half of :mod:`mcuhome.model.context`. That module is the context
*format* — the manifest as data, and the normative ID rule every party
computes the same value with. This one is everything that touches a
filesystem: creating a base context (writing the request ``context.yaml``,
the model, the public signing key and the patches), locking it (hashing
what is in it and writing the result ``manifest.yaml``), reading either
document back, and the server-side integrity check. The two documents are
the ``lock-context`` split of ADR 0018's amendment: the client writes the
request, the locking party writes the result.

ADR 0020 puts the two in different packages, and the ID rule is the
reason. The build server recomputes a context ID from bytes it received
(ADR 0019 §8) and must not carry build logic to do it; a workbench
creates contexts and needs all of this. Splitting here is what lets the
first depend on the second's vocabulary without its machinery — and, as
a side effect, keeps a YAML parser out of the package whose only job is
to be identical everywhere.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ruamel.yaml import YAML, YAMLError

from mcuhome.model.context import (
    BACKEND_DIR,
    CONTEXT_FILE,
    CONTEXT_VERSION,
    MANIFEST_FILE,
    MODEL_FILE,
    PATCHES_DIR,
    SIGNING_KEY_FILE,
    ContainerPin,
    ContextFile,
    ContextManifest,
    ContextRequest,
    SdkPin,
    context_id,
    validate_manifest,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file
from mcuhome.model.model import DeviceModel
from mcuhome.workbench.signing import looks_like_p256_public_key

__all__ = [
    "ContextFormatVersionError",
    "ContextVerification",
    "FileMismatch",
    "create_context",
    "lock_context",
    "read_context_manifest",
    "read_context_request",
    "verify_context",
    "write_context_manifest",
    "write_context_request",
]

_LAYER_NAME = re.compile(r"[a-z][a-z0-9_-]*\Z")
_PATCH_NAME = re.compile(r"[0-9]{4}-[A-Za-z0-9._-]+\.patch\Z")


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
    :class:`~mcuhome.model.errors.BuildError`, so a caller that does not care
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
#
# The hash itself is :func:`mcuhome.model.hashes.sha256_file`, one package
# down. It is not defined here because the build server recomputes it
# without carrying any of this module, and three private copies of it is
# how the two sides of §3.3 start disagreeing about what "the hash of a
# file" means.


def _context_files(root: Path) -> tuple[ContextFile, ...]:
    """Every regular file under *root* that is context content, hashed.

    Neither context document — ``manifest.yaml`` (the list itself) nor
    ``context.yaml`` (the request, whose never-hashed fields would leak
    into the identity through the back door) — is content, and neither is
    the backend-written ``.mcuhome/`` runtime directory
    (build-container-contract.md §3.2). So none of them is listed, and by
    way of that none of them can influence the ID.
    """
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in (MANIFEST_FILE, CONTEXT_FILE) or relative.split("/", 1)[0] == BACKEND_DIR:
            continue
        entries.append(ContextFile(path=relative, sha256=sha256_file(path)))
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


def _format_created(created: datetime) -> str:
    """*created* as the ``2026-08-10T09:00:00Z`` form ``context.yaml`` carries.

    A naive datetime is read as UTC; an aware one is converted to it. The
    value is the caller's, never a clock this function reads — that is what
    keeps two creations of the same request byte-identical (ADR 0018's
    ``created`` is the only field allowed to differ, and only because it is
    an explicit argument).
    """
    moment = created if created.tzinfo else created.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_context(
    model: DeviceModel,
    *,
    out_dir: Path,
    sdk: SdkPin,
    container: ContainerPin,
    signing_pub: str,
    created: datetime,
    patches_dir: Path | None = None,
) -> ContextRequest:
    """Build a base context directory from a resolved device model.

    Writes the *request* — the client-owned half of a context after the
    ``lock-context`` split (ADR 0018's 2026-08-09 amendment,
    build-container-contract.md §3.2). Concretely, into *out_dir*:

    - ``model/device-model.json`` — the canonical model, verbatim;
    - ``keys/signing.pub`` — *signing_pub*, the **public** half of the
      user's MCUboot key, which became context content in the amendment
      (the private half must never reach a build — ADR 0015 decision 8,
      so a value that is not a P-256 *public* key in PEM form is refused);
    - the patches under *patches_dir*, laid out as ``<layer>/NNNN-name.patch``;
    - ``context.yaml`` — the pins and the intent, written last.

    It writes **no** ``manifest.yaml``: the ``files`` integrity list and
    the context ``id`` do not exist until the file set is final, and the
    party that locks the context computes them (``lock_context``). *sdk*
    and *container* arrive already resolved to exact pins — resolving a
    constraint to them (``mcuhome.workbench.resolve_pins``) is a separate
    step, deliberately not this one.

    *out_dir* has to be new or empty: a context is created from scratch so
    that its later integrity list covers everything in it, which it cannot
    for files this function did not put there. *created* is an explicit
    argument and the only thing two creations of the same inputs may
    differ in — nothing here reads a clock, so identical inputs yield
    byte-identical files.
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
    if not looks_like_p256_public_key(signing_pub):
        raise BuildError(
            "The key given for the context is not an ECDSA P-256 public key in PEM form.",
            hint=(
                "keys/signing.pub carries the public half of your MCUboot signing key "
                "(ADR 0015 decision 8) — never the private half, which must never reach "
                "a build. `mcuhome public-key` writes exactly this file."
            ),
        )

    model_path = out_dir / MODEL_FILE
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(model.to_json(), encoding="utf-8")

    key_path = out_dir / SIGNING_KEY_FILE
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(signing_pub, encoding="utf-8")

    if patches_dir is not None:
        _copy_patches(patches_dir, out_dir / PATCHES_DIR)

    request = ContextRequest(
        sdk=sdk,
        container=container,
        board=model.device.board,
        created=_format_created(created),
    )
    write_context_request(request, out_dir=out_dir)
    return request


def lock_context(out_dir: Path) -> ContextManifest:
    """Freeze a created context: hash its files and write ``manifest.yaml``.

    The write-side counterpart of :func:`create_context`. Creating writes
    the request (``context.yaml``); locking turns that request plus the
    now-final file set into the integrity *record* — the ``files`` list
    and the context ``id`` (ADR 0018 amendment,
    build-container-contract.md §3.2). It reads the pins back out of
    ``context.yaml`` rather than taking them again, so the manifest can
    only ever restate what the request already committed the session to.

    A remote build server does this from the bytes it received off a
    socket; a local build method does it over the directory the workbench
    just created (ADR 0020 decision 6). Both compute the same ID over the
    same files with :func:`mcuhome.model.context.context_id`, which is the
    whole point of a content-addressed identity — so this function is the
    local side of "both parties compute the same value at lock-context"
    (build-container-contract.md §3.3).
    """
    out_dir = Path(out_dir)
    request = read_context_request(out_dir / CONTEXT_FILE)
    files = _context_files(out_dir)
    manifest = ContextManifest(
        sdk=request.sdk,
        container=request.container,
        board=request.board,
        files=files,
        id=context_id(
            container_digest=request.container.digest,
            sdk_sha256=request.sdk.sha256,
            board=request.board,
            files=files,
        ),
    )
    write_context_manifest(manifest, out_dir=out_dir)
    return manifest


# --------------------------------------------------------------------------
# The request document on disk
# --------------------------------------------------------------------------


def write_context_request(request: ContextRequest, *, out_dir: Path) -> Path:
    """Write ``context.yaml`` into *out_dir* and return its path.

    Deterministic by construction: :meth:`ContextRequest.to_dict` fixes
    the key order and ``created`` is already a string, so ruamel emits the
    same bytes for the same request every time. The YAML bytes are
    presentation and never identity — the request carries no ID, and a
    reader re-parses the values rather than hashing the file. Line
    wrapping is switched off so a pin stays on one line: a 71-character
    ``digest: sha256:…`` value exceeds ruamel's default width and would
    otherwise fold across two lines, which a stricter reader of §3.3.1's
    lexical form should not have to reassemble.
    """
    path = out_dir / CONTEXT_FILE
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.width = 4096
    try:
        with path.open("w", encoding="utf-8") as handle:
            yaml.dump(request.to_dict(), handle)
    except OSError as error:
        raise BuildError(
            f"The context request {path} cannot be written: {error.strerror}.",
            hint="pick a writable location for the context directory",
        ) from error
    return path


def read_context_request(path: Path) -> ContextRequest:
    """Load a context ``context.yaml``, or refuse in plain language.

    Checks shape and the format version, the same way
    :func:`read_context_manifest` does for the lock result. It does not
    check truth — whether the pins match what a backend actually obtained
    is the backend's cross-check (ADR 0018 amendment; ADR 0019 §8), not
    this reader's.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"MCUHome cannot read the context request {path}: {error.strerror}.",
            hint=(
                f"a build context carries a {CONTEXT_FILE} at its top level — "
                "point at a directory create_context wrote"
            ),
        ) from error
    try:
        data = YAML(typ="safe").load(text)
    except YAMLError as error:
        problem = str(error).splitlines()[0] if str(error) else "unreadable syntax"
        raise BuildError(
            f"The context request {path} is not valid YAML ({problem}).",
            hint="it is builder output — recreate the context rather than editing it",
        ) from error
    if not isinstance(data, dict):
        raise BuildError(
            f"The context request {path} does not describe a context.",
            hint="it is builder output — recreate the context rather than editing it",
        )

    found = data.get("context")
    if found != CONTEXT_VERSION:
        raise ContextFormatVersionError(
            f"The context request {path} has format version {found!r}, and this "
            f"builder implements version {CONTEXT_VERSION}.",
            hint=(
                "the context format is a versioned contract: a mismatch is a refusal "
                "that names both numbers, never a guess. Recreate the context with a "
                "matching mcuhome version."
            ),
            found=found,
        )
    try:
        return ContextRequest.from_dict(data)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise BuildError(
            f"The context request {path} is missing something this builder needs: {error}.",
            hint=(
                f"it states context format {CONTEXT_VERSION}, so this is a truncated "
                "or hand-edited file rather than a version mismatch. Recreate the "
                "context."
            ),
        ) from error


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
    it would be attributed to. Raises :class:`~mcuhome.model.errors.BuildError`
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
