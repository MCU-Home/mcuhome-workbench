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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcuhome.model.context import (
    BACKEND_DIR,
    CONTEXT_FILE,
    CONTEXT_VERSION,
    MANIFEST_FILE,
    MODEL_FILE,
    PATCHES_DIR,
    SIGNING_KEY_FILE,
    ContextFile,
    ContextManifest,
    ContextRequest,
    EnvironmentPin,
    SdkPin,
    context_id,
    environment_digest,
    validate_manifest,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file
from mcuhome.model.model import DeviceModel
from ruamel.yaml import YAML, YAMLError

from mcuhome.workbench.resolve_pins import SDK_ANY, resolve_sdk
from mcuhome.workbench.signing import looks_like_p256_public_key

__all__ = [
    "ContextFormatVersionError",
    "ContextVerification",
    "FileMismatch",
    "context_facts",
    "create_build_context",
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


def _content_paths(root: Path) -> list[str]:
    """Context-relative paths of everything under *root* that is content.

    Neither context document — ``manifest.yaml`` (the list itself) nor
    ``context.yaml`` (the request, whose never-hashed fields would leak
    into the identity through the back door) — is content, and neither is
    the backend-written ``.mcuhome/`` runtime directory
    (build-container-contract.md §3.2). So none of them is listed, and by
    way of that none of them can influence the ID.
    """
    paths = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in (MANIFEST_FILE, CONTEXT_FILE) or relative.split("/", 1)[0] == BACKEND_DIR:
            continue
        paths.append(relative)
    paths.sort()
    return paths


def _context_files(root: Path) -> tuple[ContextFile, ...]:
    """Every regular file under *root* that is context content, hashed."""
    return tuple(
        ContextFile(path=name, sha256=sha256_file(root / name)) for name in _content_paths(root)
    )


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
    build_environment: EnvironmentPin,
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
    party that locks the context computes them (``lock_context``).

    Both pins arrive **already resolved**, and for the same reason: this
    function writes a document, it does not decide what goes in it.
    Resolving the SDK constraint is
    :mod:`mcuhome.workbench.resolve_pins`, resolving the build
    environment is :mod:`mcuhome.workbench.resolve_env`, and both happen
    before a context directory exists — a refusal from either costs
    nothing then.

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
                "— never the private half, which must never reach "
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
        build_environment=build_environment,
        board=model.device.board,
        created=_format_created(created),
    )
    write_context_request(request, out_dir=out_dir)
    return request


def create_build_context(
    model: DeviceModel,
    *,
    out_dir: Path,
    sdk_sources: Sequence[Path],
    build_environment: EnvironmentPin,
    signing_pub: str,
    created: datetime | None = None,
    constraint: str = SDK_ANY,
) -> ContextRequest:
    """Resolve the SDK pin and write a fresh base context at *out_dir*.

    The seam **both** container-shaped build methods create a context
    through (E65). ``local`` and ``remote`` differ in everything after
    this point — one starts a container and locks the context itself, the
    other sends the directory to a build server that locks it — and in
    what a context *is* they do not differ at all: the resolved pin, the
    canonical model, the public signing key, the patches. Two callers
    assembling that by hand is two places for the pin and the layout to
    drift apart, under an identity that claims they cannot have.

    *out_dir* is **removed if it exists**, because :func:`create_context`
    requires an empty directory and a build method's context directory is
    its own scratch area, rebuilt every run. Callers pass a path they own
    (``<work root>/context``), never a directory a user named.

    *created* defaults to now. It is the one field two creations of the
    same inputs may differ in (ADR 0018) and it is outside the identity,
    so a caller that wants byte-identical output states it.

    The two never-hashed fields of the pin — the intent and the location
    hint — are rendered by :class:`~mcuhome.workbench.resolve_pins.SdkResolution`
    rather than here, and both are legitimately empty for a locally
    resolved package: an empty ``mcuhome.constraint`` is PEP 440's own
    any-version specifier, and a ``file://`` hint would carry this
    machine's filesystem layout into a document uploaded to a build
    server. The server accepts both empty; absence, not emptiness, is
    what a reader refuses as malformed.
    """
    found = resolve_sdk(sdk_sources, constraint=constraint)
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    return create_context(
        model,
        out_dir=out_dir,
        build_environment=build_environment,
        sdk=SdkPin(
            constraint=found.intent,
            version=found.package.version,
            url=found.url,
            sha256=found.package.sha256,
        ),
        signing_pub=signing_pub,
        created=created or datetime.now(UTC),
    )


def lock_context(out_dir: Path) -> ContextManifest:
    """Freeze a created context: hash its files and write ``manifest.yaml``.

    The write-side counterpart of :func:`create_context`. Creating writes
    the request (``context.yaml``); locking turns that request and the
    now-final file set into the integrity *record* — the ``files`` list
    and the context ``id`` (ADR 0018 amendment,
    build-container-contract.md §3.2). It reads the request back out of
    ``context.yaml`` rather than taking it again, so the manifest can
    only ever restate what the request already committed the session to.

    **It takes nothing but the directory**, which is the shape the
    client-side pin bought: everything the manifest states is either in
    the request or derivable from the files, so whoever locks a context
    adds no information to it and cannot. The previous format needed the
    locking party to supply the container it had chosen, and that made
    two backends able to produce two different manifests from one
    request.

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
        build_environment=request.build_environment,
        board=request.board,
        files=files,
        id=context_id(
            sdk_sha256=request.sdk.sha256,
            environment_digest=request.build_environment.digest,
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
    wrapping is switched off so a pin stays on one line: a 64-character
    ``sha256:`` value plus its key exceeds ruamel's default width and
    would otherwise fold across two lines, which a stricter reader of
    §3.3.1's lexical form should not have to reassemble.
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
        request = ContextRequest.from_dict(data)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise BuildError(
            f"The context request {path} is missing something this builder needs: {error}.",
            hint=(
                f"it states context format {CONTEXT_VERSION}, so this is a truncated "
                "or hand-edited file rather than a version mismatch. Recreate the "
                "context."
            ),
        ) from error
    # The one field of the request a reader can get *wrong* rather than
    # miss, and the one an identity is computed over: checked here so the
    # request reader is as strict as read_context_manifest, which gets
    # the same check through validate_manifest.
    environment_digest(request.build_environment.reference)
    return request


# --------------------------------------------------------------------------
# The manifest on disk
# --------------------------------------------------------------------------


def write_context_manifest(manifest: ContextManifest, *, out_dir: Path) -> Path:
    """Write ``manifest.yaml`` into *out_dir* and return its path.

    The YAML bytes are presentation, never identity: the ID was computed
    over the canonical JSON form before this function ran, and a reader
    re-parses the values rather than hashing the file.

    Line wrapping is switched off for the same reason as in
    :func:`write_context_request`, and this is where it actually bit: a
    ``sha256:`` digest is 71 characters, ``container.digest`` lands past
    ruamel's default width, and the emitter folded it onto a second
    line. That is legal YAML which every conforming parser folds back —
    the round trip through this module never noticed — and it is still
    the wrong thing to write. ``manifest.yaml`` is read by build
    containers this project does not write, in languages it does not
    choose (contract §1.1's third-party program), and §3.3.1 has them
    **refuse** a hash rendered any other way rather than repair it. A
    one-line value cannot be read as two. The build server's own emitter
    has said so since it was written; the reference emitter did not.
    """
    path = out_dir / MANIFEST_FILE
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.width = 4096
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


def context_facts(root: Path) -> dict[str, Any]:
    """What a person wants to know about the context at *root*.

    Not a document and part of no protocol: a build that says "context"
    has just decided which SDK the firmware is built from, which build
    environment compiles it and whether anything patches that
    environment — decisions worth one line on a terminal rather than
    only in a file nobody opens. Read back off the directory
    instead of remembered by the caller, so what is reported is what a
    build environment will actually receive.

    Every key is optional to a consumer and the set is append-only: this
    is display material, and a caller that renders what it recognizes
    must not break when a later version knows more. ``id`` is present
    only once the context is locked — a base context on its way to a
    build server has no identity yet, because computing it is that
    server's act.
    """
    root = Path(root)
    facts: dict[str, Any] = {}
    manifest_path = root / MANIFEST_FILE
    request_path = root / CONTEXT_FILE
    if manifest_path.is_file():
        manifest = read_context_manifest(manifest_path)
        facts["id"] = manifest.id
        pin, environment, board = manifest.sdk, manifest.build_environment, manifest.board
    else:
        request = read_context_request(request_path)
        pin, environment, board = request.sdk, request.build_environment, request.board
    paths = _content_paths(root)
    facts.update(
        sdk=pin.version,
        sdk_sha256=pin.sha256,
        build_environment=environment.reference,
        board=board,
        files=len(paths),
        patches=[
            name[len(PATCHES_DIR) + 1 :] for name in paths if name.startswith(f"{PATCHES_DIR}/")
        ],
    )
    return facts


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
            sdk_sha256=manifest.sdk.sha256,
            environment_digest=manifest.build_environment.digest,
            board=manifest.board,
            files=present,
        ),
        mismatches=mismatches,
    )
