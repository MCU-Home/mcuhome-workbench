# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build context on disk: creating one, reading it back, verifying it.

The other half of :mod:`mcuhome.model.context`. That module is the context
*format* — the manifest as data, and the normative ID rule every party
computes the same value with. This one is everything that touches a
filesystem: creating a base context (writing the generator declaration
``build-context.json``, the request ``context.yaml``, the model, the
public signing key and the patches), locking it (hashing
what is in it and writing the result ``manifest.yaml``), reading either
document back, and the server-side integrity check. The two documents are
the ``lock-context`` split: the client writes the request, the locking
party writes the result.

The two live in different packages, and the ID rule is the reason. A
build server recomputes a context ID from bytes it received off a socket
and must not carry build logic to do it; a workbench creates contexts and
needs all of this. Splitting here is what lets the first depend on the
second's vocabulary without its machinery — and, as a side effect, keeps
a YAML parser out of the package whose only job is to be identical
everywhere.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcuhome.model.buildenvironment import DEFAULT_BUILD_TOOLS, DEFAULT_BUILD_WORKSPACE
from mcuhome.model.context import (
    BACKEND_DIR,
    BUILD_CONTEXT_FILE,
    CONTEXT_FILE,
    CONTEXT_VERSION,
    MANIFEST_FILE,
    MODEL_FILE,
    PATCHES_DIR,
    SIGNING_KEY_FILE,
    ContextEnvironment,
    ContextFile,
    ContextManifest,
    ContextRequest,
    DeveloperEnvironment,
    GeneratorEntry,
    SdkPin,
    context_id,
    format_generator_chain,
    parse_generator_chain,
    validate_manifest,
    validate_request,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file
from mcuhome.model.model import DeviceModel
from mcuhome.model.sdkindex import DEFAULT_SDK
from ruamel.yaml import YAML, YAMLError

from mcuhome.workbench import __version__
from mcuhome.workbench.packageregistry import PackageRegistry, RegistrySource
from mcuhome.workbench.resolve_pins import resolve_environment, resolve_sdk, sdk_constraint
from mcuhome.workbench.signing import is_p256_public_key

__all__ = [
    "DEVELOPER_SDK_FACT",
    "GENERATOR_PRODUCT",
    "ContextFormatVersionError",
    "ContextVerification",
    "FileMismatch",
    "create_build_context",
    "create_context",
    "generator_chain",
    "lock_context",
    "read_context_facts",
    "read_context_manifest",
    "read_context_request",
    "read_generator_chain",
    "verify_context",
    "write_build_context",
    "write_context_manifest",
    "write_context_request",
]

#: What :func:`read_context_facts` reports as the SDK of a development build.
#: The context states no version and no hash there, and a renderer that
#: printed the empty string would say nothing where it means to say where
#: the code came from.
DEVELOPER_SDK_FACT = "from the workspace you are building in"

_LAYER_NAME = re.compile(r"[a-z][a-z0-9_-]*\Z")
_PATCH_NAME = re.compile(r"[0-9]{4}-[A-Za-z0-9._-]+\.patch\Z")


class ContextFormatVersionError(BuildError):
    """The manifest states a ``context`` format version nothing here implements.

    A type of its own, and for this one refusal only, because a build
    environment answers it differently from every other manifest this
    package cannot read: the build context format
    (mcuhome-sdk ``docs/spec/build-context-format.md`` §10) says a reader
    that does not implement the version it finds **refuses**, and the
    build environment specification calls that answer ``unsupported``
    rather than ``failure`` — the environment is refusing a document
    written to a specification it does not have, which an orchestrator can
    act on by choosing a different environment, and nothing about the
    context is broken.

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
# how the two sides of a build start disagreeing about what "the hash of
# a file" means.


def _content_paths(root: Path) -> list[str]:
    """Context-relative paths of everything under *root* that is content.

    Neither context document — ``manifest.yaml`` (the list itself) nor
    ``context.yaml`` (the request, whose never-hashed fields would leak
    into the identity through the back door) — is content, and neither is
    the backend-written ``.mcuhome/`` runtime directory of an earlier
    design. So none of them is listed, and by way of that none of them can
    influence the ID.

    ``build-context.json`` is not on that list and is content like any
    other file: it names the tool that wrote the context, which is what a
    build environment's generator constraint is checked against, and a
    file that decides who may run a build belongs inside the identity
    that build is claimed under.
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
    keeps two creations of the same request byte-identical — ``created``
    is the only field allowed to differ, and only because it is an
    explicit argument.
    """
    moment = created if created.tzinfo else created.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_context(
    model: DeviceModel,
    *,
    out_dir: Path,
    sdk: SdkPin,
    build_environment: ContextEnvironment,
    signing_pub: str,
    created: datetime,
    patches_dir: Path | None = None,
) -> ContextRequest:
    """Build a base context directory from a resolved device model.

    Writes the *request* — the client-owned half of a context after the
    ``lock-context`` split. Concretely, into *out_dir*:

    - ``build-context.json`` — the generator declaration, the one file
      of a context the build environment specification names itself;
    - ``model/device-model.json`` — the canonical model, verbatim;
    - ``keys/signing.pub`` — *signing_pub*, the **public** half of the
      user's MCUboot key, which became context content in the amendment
      (the private half must never reach a build, so a value that is not
      a P-256 *public* key in PEM form is refused);
    - the patches under *patches_dir*, laid out as ``<layer>/NNNN-name.patch``;
    - ``context.yaml`` — the pins and the intent, written last.

    It writes **no** ``manifest.yaml``: the ``files`` integrity list and
    the context ``id`` do not exist until the file set is final, and the
    party that locks the context computes them (``lock_context``).

    Both pins arrive **already resolved**, and for the same reason: this
    function writes a document, it does not decide what goes in it.
    Resolving the SDK constraint is
    :mod:`mcuhome.workbench.resolve_pins`, and both the SDK pin and
    the environment's package pins are resolved there — both happen
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
    if not is_p256_public_key(signing_pub):
        raise BuildError(
            "The key given for the context is not an ECDSA P-256 public key in PEM form.",
            hint=(
                "keys/signing.pub carries the public half of your MCUboot signing key "
                "— never the private half, which must never reach "
                "a build. `mcuhome public-key` writes exactly this file."
            ),
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    write_build_context(out_dir, generator=generator_chain())

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
    work_root: Path,
    sdk_sources: Sequence[Path],
    signing_pub: str,
    workspace_sources: Sequence[Path] = (),
    tools_sources: Sequence[Path] = (),
    sdk_max_bytes: int | None = None,
    created: datetime | None = None,
    constraint: str | None = None,
    registry: RegistrySource | None = None,
    hosts: Callable[[str], PackageRegistry] | None = None,
    platform: str | None = None,
    developer: bool = False,
    on_line: Callable[[str], None] | None = None,
) -> ContextRequest:
    """Resolve every pin and write a fresh base context at *out_dir*.

    The seam **every** build creates a context through. They
    differ in everything after this point — one starts a container and
    locks the context itself, one sends the directory to a build server
    that locks it, one runs an entry point from a store — and in what a
    context *is* they do not differ at all: the resolved pins, the
    canonical model, the public signing key, the patches. Two callers
    assembling that by hand is two places for the pins and the layout to
    drift apart, under an identity that claims they cannot have.

    **Every pin is resolved here, and each one follows the last.** The
    SDK constraint resolves to one release; that release's own meta file
    states which range of build workspaces it was built and tested with,
    the newest published one inside that range wins, and its meta file
    states the range of build tools
    (:func:`~mcuhome.workbench.resolve_pins.resolve_environment`). A
    device that says nothing therefore gets an SDK and an environment
    that were declared to belong together, and one that pins either
    (``sources.build_workspace``, ``sources.build_tools``) overrides that
    package alone — outside the declared range too, with a note on
    *on_line* rather than a refusal.

    *work_root* is a directory this function may use as scratch; the SDK
    package is unpacked there to read its meta file out of bytes that
    were verified against the pin — under *sdk_max_bytes*, the operator's
    bound on that unpacking, which is the store's own default when nobody
    moved it.

    *hosts* opens a registry for a base domain other than the SDK's, for
    a device that points one package at another package host; *registry*
    is the client for the SDK's own.

    *workspace_sources* and *tools_sources* are the operator directories
    the two environment packages are looked up in, and *sdk_sources* the
    SDK's. **One kind, one set of directories**: no kind is ever looked
    for under another's, so empty means there is no operator directory
    for that package and it is resolved through the registry. A machine
    that keeps the gigabyte-sized environment packages somewhere else
    names that place; one that keeps all three together names it three
    times, which is the statement it is actually making.

    *out_dir* is **removed if it exists**, because :func:`create_context`
    requires an empty directory and a build's context directory is
    its own scratch area, rebuilt every run. Callers pass a path they own
    (``<work root>/context``), never a directory a user named.

    *created* defaults to now. It is the one field two creations of the
    same inputs may differ in and it is outside the identity, so a caller
    that wants byte-identical output states it.

    *constraint* left unstated is not "any version": it is what the
    device itself says, through
    :func:`~mcuhome.workbench.resolve_pins.sdk_constraint` over its
    ``sources.sdk`` reference — the version it named, or the SDK minor
    this workbench was released alongside when it named none. A device is
    therefore neither frozen onto whatever was current the day it was
    created nor carried forward onto an SDK this workbench has never
    seen. That function decides the pre-release rule with it, because the
    two are one decision: the default names MCUHome's own line, whose
    releases are all pre-releases today, while a version the device
    states is held to the ordinary rule. *registry* is the second tier
    the resolution may fall through to; without one, only the operator's
    directories are searched.

    The two never-hashed fields of the pin — the intent and the location
    hint — are rendered by :class:`~mcuhome.workbench.resolve_pins.SdkResolution`
    rather than here, and both are legitimately empty for a locally
    resolved package: an empty ``mcuhome.constraint`` is PEP 440's own
    any-version specifier, and a ``file://`` hint would carry this
    machine's filesystem layout into a document uploaded to a build
    server. The server accepts both empty; absence, not emptiness, is
    what a reader refuses as malformed.
    """
    if developer:
        # Nothing to resolve and nothing to fetch: this build compiles a
        # checkout, and the format says so in the one way it can — the
        # word, and the empty SDK hash that travels with it. Refusing an
        # override here rather than ignoring it, because `sources.sdk`
        # names a package to fetch and there is no package in this build.
        # Every `sources.*` entry names a package to fetch, and a
        # development build fetches nothing: the SDK is the workspace's
        # manifest repository and the environment is the workspace and
        # the person's own PATH. Honouring one would fetch a package
        # nothing then builds; ignoring it would build something other
        # than what the device says.
        if model.sources.container_image:
            # Not a package, and refused for the same reason: a
            # development build starts no container, so the image the
            # device pins would name nothing this build uses. A pin that
            # is quietly dropped is worse than one that is refused —
            # the firmware would look exactly like the pinned build.
            raise BuildError(
                f"This device states sources.container_image: "
                f'"{model.sources.container_image}", and this build compiles a '
                f"workspace you maintain.",
                hint=(
                    "a development build runs on this machine with your own tools "
                    "and starts no container, so no image can name it — remove "
                    "sources.container_image from the device, or unset "
                    "build.dev_workspace to build in the environment it pins"
                ),
            )
        for key, stated, default in (
            ("sdk", model.sources.sdk, DEFAULT_SDK),
            ("build_workspace", model.sources.build_workspace, DEFAULT_BUILD_WORKSPACE),
            ("build_tools", model.sources.build_tools, DEFAULT_BUILD_TOOLS),
        ):
            if stated != default:
                raise BuildError(
                    f'This device states sources.{key}: "{stated}", and this build '
                    f"compiles a workspace you maintain.",
                    hint=(
                        "a development build compiles that workspace and the SDK "
                        "checkout in it, and no package reference can name either — "
                        f"remove sources.{key} from the device, or unset "
                        "build.dev_workspace to build against the packages it names"
                    ),
                )
        out_dir = Path(out_dir)
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        return create_context(
            model,
            out_dir=out_dir,
            build_environment=DeveloperEnvironment(),
            sdk=SdkPin(constraint="", version="", url="", sha256=""),
            signing_pub=signing_pub,
            created=created or datetime.now(UTC),
        )
    prereleases = None
    if constraint is None:
        constraint, prereleases = sdk_constraint(model.sources.sdk)
    found = resolve_sdk(
        sdk_sources, constraint=constraint, prereleases=prereleases, registry=registry
    )
    build_environment = resolve_environment(
        workspace=model.sources.build_workspace,
        tools=model.sources.build_tools,
        sdk_source=model.sources.sdk,
        sdk=found,
        sources=sdk_sources,
        workspace_sources=workspace_sources,
        tools_sources=tools_sources,
        max_bytes=sdk_max_bytes,
        work_root=Path(work_root),
        registry=registry,
        hosts=hosts,
        platform=platform,
        on_line=on_line,
    )
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
    and the context ``id``. It reads the request back out of
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
    socket; a local build does it over the directory the workbench
    just created. Both compute the same ID over the same files with
    :func:`mcuhome.model.context.context_id`, which is the whole point of a
    content-addressed identity — so this function is the local side of
    "both parties compute the same value at lock-context".
    """
    out_dir = Path(out_dir)
    request = read_context_request(out_dir / CONTEXT_FILE)
    files = _context_files(out_dir)
    manifest = ContextManifest(
        sdk=request.sdk,
        # Without the location hints: the lock states what is in the
        # context, and where the bytes were found is the request's.
        build_environment=request.build_environment.without_urls(),
        board=request.board,
        files=files,
        id=context_id(
            sdk_sha256=request.sdk.sha256,
            environment=request.build_environment,
            board=request.board,
            files=files,
        ),
    )
    write_context_manifest(manifest, out_dir=out_dir)
    return manifest


# --------------------------------------------------------------------------
# The request document on disk
# --------------------------------------------------------------------------


#: The workbench's own name in a generator chain. Its distribution name,
#: because that is the name a build environment writes into its version
#: constraint and the name a reader can look up.
GENERATOR_PRODUCT = "mcuhome-workbench"


def generator_chain() -> str:
    """The ``generator`` value a context this workbench creates carries.

    One entry, because the workbench creates contexts from a device model
    rather than modifying somebody else's. A tool that changes a context
    afterwards prepends itself instead — the chain is read most recent
    first (:func:`~mcuhome.model.context.format_generator_chain`).
    """
    return format_generator_chain((GeneratorEntry(GENERATOR_PRODUCT, __version__),))


def write_build_context(out_dir: Path, *, generator: str) -> Path:
    """Write ``build-context.json`` into *out_dir* and return its path.

    The one file of a context whose name and content the build
    environment specification fixes; everything else in a context is this
    workbench's format. It carries the generator chain and, for now,
    nothing else — the specification says "at least", so a later key can
    join it without any environment having to change.

    Deterministic bytes for deterministic inputs, like every other file a
    context is created from: a fixed key order, two-space indent, one
    trailing newline. It is an integrity entry and therefore part of the
    context ID, so a serializer that reordered keys would move the
    identity of a build that did not change.
    """
    path = out_dir / BUILD_CONTEXT_FILE
    document = {"generator": generator}
    try:
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"The generator declaration {path} cannot be written: {error.strerror}.",
            hint="pick a writable location for the context directory",
        ) from error
    return path


def read_generator_chain(path: Path) -> tuple[GeneratorEntry, ...]:
    """The generator chain out of a context's ``build-context.json``.

    Refuses in plain language rather than raising a parser's error,
    because the answer decides which build environments may run this
    context: a context whose generator cannot be read is refused before
    an environment is started, not carried into a build that would then
    fail somewhere unrelated.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"MCUHome cannot read the generator declaration {path}: {error.strerror}.",
            hint=(
                f"a build context carries a {BUILD_CONTEXT_FILE} at its top level — "
                "point at a directory create_context wrote"
            ),
        ) from error
    try:
        data = json.loads(text)
    except ValueError as error:
        raise BuildError(
            f"The generator declaration {path} is not readable JSON: {error}.",
            hint=f"{BUILD_CONTEXT_FILE} is one JSON object carrying a generator",
        ) from error
    if not isinstance(data, dict):
        raise BuildError(
            f"The generator declaration {path} is not a JSON object.",
            hint=f"{BUILD_CONTEXT_FILE} is one JSON object carrying a generator",
        )
    return parse_generator_chain(data.get("generator"))


def write_context_request(request: ContextRequest, *, out_dir: Path) -> Path:
    """Write ``context.yaml`` into *out_dir* and return its path.

    Deterministic by construction: :meth:`ContextRequest.to_dict` fixes
    the key order and ``created`` is already a string, so ruamel emits the
    same bytes for the same request every time. The YAML bytes are
    presentation and never identity — the request carries no ID, and a
    reader re-parses the values rather than hashing the file. Line
    wrapping is switched off so a pin stays on one line: a 64-character
    ``sha256:`` value plus its key exceeds ruamel's default width and
    would otherwise fold across two lines, which a stricter reader has no
    reason to reassemble.
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
    is the backend's own cross-check, not this reader's.
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
    # The fields of the request a reader can get *wrong* rather than
    # miss, and the ones an identity is computed over: checked here so
    # the request reader is as strict as read_context_manifest, which
    # gets the same check through validate_manifest. The pair rule is
    # among them — a document that names no environment and pins an SDK
    # anyway is refused here rather than read and half-believed.
    validate_request(request)
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
    64-character hash plus its key lands past ruamel's default width, and
    the emitter folded it onto a second line. That is legal YAML which
    every conforming parser folds back — the round trip through this
    module never noticed — and it is still the wrong thing to write.
    ``manifest.yaml`` is read by build environments this project does not
    write, in languages it does not choose, and a strict reader is
    entitled to **refuse** a hash rendered any other way rather than
    repair it. A one-line value cannot be read as two. The build server's
    own emitter has said so since it was written; the reference emitter
    did not.
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


def read_context_facts(root: Path) -> dict[str, Any]:
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
        # A developer context pins no SDK package and states no hash, so
        # the version a renderer would print is empty. Saying where the
        # SDK came from instead is the honest line and the one a person
        # needs: this build compiles the checkout they are working in.
        sdk=DEVELOPER_SDK_FACT if isinstance(environment, DeveloperEnvironment) else pin.version,
        sdk_sha256=pin.sha256,
        build_environment=environment.described(),
        board=board,
        files=len(paths),
        patches=[
            name[len(PATCHES_DIR) + 1 :] for name in paths if name.startswith(f"{PATCHES_DIR}/")
        ],
    )
    if not isinstance(environment, DeveloperEnvironment):
        # The two halves apart as well as together, spelled the same way:
        # a renderer that wants one line takes build_environment, one that
        # wants a row per package takes these, and neither has to take the
        # other one's shape apart. A developer context has no packages to
        # take apart, and an entry with empty members would be read as one.
        facts["build_workspace"] = f"{environment.workspace.name} {environment.workspace.version}"
        facts["build_tools"] = f"{environment.tools.name} {environment.tools.version}"
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

    def to_dict(self) -> dict[str, Any]:
        """This one disagreement as a document, JSON-ready."""
        return {
            "path": self.path,
            "declared_sha256": self.declared_sha256,
            "actual_sha256": self.actual_sha256,
        }


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

    def to_dict(self) -> dict[str, Any]:
        """This verification as a document, JSON-ready.

        ``context_id`` is the *declared* id — the one the manifest
        states, out of :attr:`declared_id` — because that is the identity
        a caller checked the context against; :attr:`actual_id` answers
        what the bytes present actually hash to, under its own key.
        """
        return {
            "ok": self.ok,
            "root": str(self.root),
            "context_id": self.declared_id,
            "actual_id": self.actual_id,
            "mismatches": [mismatch.to_dict() for mismatch in self.mismatches],
        }


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
            environment=manifest.build_environment,
            board=manifest.board,
            files=present,
        ),
        mismatches=mismatches,
    )
