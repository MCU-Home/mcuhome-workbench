# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Every build, behind one interface.

A build runs at a **target** — ``local`` drives a build environment on
this machine through the build environment specification, ``remote``
drives a build server through the session protocol — and a local one runs
in a **mode**, in a build container or as a child process against an
unpacked environment. The two axes differ in almost everything, and in
exactly the thing a caller cares about they do not differ at all: every
one of them delivers an **unsigned** image plus a build report, and the
signature is a separate host-side step afterwards. That is what makes one
interface possible rather than merely tidy.

So this module is small on purpose. :func:`build_firmware` takes a
resolved device model plus the inputs a build needs, runs the one the
target names, and answers with one :class:`BuildResult` whose meaning
does not depend on which ran: *did it succeed*, and *where are the
unsigned artifacts and the report* — enough for the shared signing step,
and enough for a caller that only wants to know whether to carry on. What
is genuinely composition-specific — a container reference, an invocation
id — travels in :attr:`BuildResult.detail`, typed as itself, so a
renderer can reach it without every consumer having to.

**Two axes, and each has its own word.** Where a build runs and how the
machine that runs it executes it are separate decisions and only the
first belongs to a caller, which is what :mod:`…buildtarget` states:
:class:`~mcuhome.workbench.buildtarget.LocalBuild` carries an
:class:`~mcuhome.workbench.buildtarget.Execution`,
:class:`~mcuhome.workbench.buildtarget.RemoteBuild` deliberately carries
none. :func:`build_firmware` takes one of those, or the name a flag or a
configuration value carried, or nothing at all — and the translation
from a name and a request into a target object happens in one place
(:func:`build_target_for`), so that the fields a target reads have
exactly one reader.

**No key of any kind can be private here.** The one field that carries
key material is :attr:`BuildRequest.signing_pub`, the PEM that becomes
``keys/signing.pub`` in a build context — the public half, and all of the
key pair a build ever sees. There is no slot a private key fits in, at
either target and by construction, which is the structural half of the
invariant that the signing key never leaves the machine ``mcuhome`` runs
on.

**Nothing here reaches outside this package.** The thing that drives a
build container is this package's own
(:mod:`mcuhome.workbench.containerbuild`), and no build runs a compiler
in this process. "A build needs a container runtime and nothing else of a
toolchain" was the claim from the start, and it is true at the level of
installed distributions: ``mcuhome-compiler`` is what a *build
environment* carries, and this package never imports it. The one host-side
call into it left is code generation for its own sake
(:mod:`mcuhome.workbench.generate`), which no build takes.

**Awaitable, because builds wait.** ``remote`` is asynchronous throughout
and a local build blocks for minutes in a subprocess, so the one
interface over them is ``async`` and the synchronous side is offloaded
to a thread. A command line wraps the whole thing in one
:func:`asyncio.run` at its entry point and its user sees nothing.

**Both targets start from a device model.** A build context is
content-addressed over the SDK package's hash, and a remote build
resolves that pin *here*, through the same resolver and the same context
writer a local build uses
(:func:`~mcuhome.workbench.resolve_pins.resolve_sdk`,
:func:`~mcuhome.workbench.contextdir.create_build_context`), from the
same source directories. There is deliberately **no** capabilities round
trip for it: the client states a version *and* a sha256, the server
resolves the version against its own sources — which may be a cache, a
package service or a private registry — and verifies the bytes it found
against the hash. Same number, other bytes is a typed refusal on that
side, never a quiet build against another SDK; and because the hash is
what the identity is computed over, none of this changes the context
format or the ID rule.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcuhome.model.artifacts import Artifact
from mcuhome.model.context import (
    BUILD_CONTEXT_FILE,
    CONTEXT_FILE,
    DeveloperEnvironment,
    format_generator_chain,
)
from mcuhome.model.errors import BuildError, ConfigError
from mcuhome.model.model import DeviceModel

from mcuhome.workbench import buildenvstore, containerbuild, subprocessbuild
from mcuhome.workbench.buildenvsession import (
    STATUS_UNSUPPORTED,
    BuildLimits,
    EnvironmentUnavailable,
    EnvironmentUnusable,
    parse_memory,
    resolve_cache_tiers,
    resolve_host_limits,
)
from mcuhome.workbench.builders import SelectedBuilder
from mcuhome.workbench.buildlock import open_build_lock
from mcuhome.workbench.buildtarget import (
    BUILD_MODES,
    BUILD_TARGETS,
    DEFAULT_BUILD_MODE,
    DEFAULT_BUILD_TARGET,
    DEFAULT_CONTAINER_PROGRAM,
    DEFAULT_CONTAINER_REPOSITORIES,
    DEFAULT_MAX_WAIT_SECONDS,
    MODE_CONTAINER,
    MODE_SUBPROCESS,
    TARGET_LOCAL,
    TARGET_REMOTE,
    BuildTarget,
    ContainerExecution,
    Execution,
    LocalBuild,
    RemoteBuild,
    SubprocessExecution,
)
from mcuhome.workbench.configuration import Setting, Settings, resolve_settings
from mcuhome.workbench.contextdir import (
    create_build_context,
    lock_context,
    read_context_facts,
    read_context_request,
    read_generator_chain,
)
from mcuhome.workbench.diagnostics import Diagnostic
from mcuhome.workbench.imgtool import BUILD_REPORT_FILE
from mcuhome.workbench.project import Project
from mcuhome.workbench.resolve_pins import (
    SDK_STAGE,
    TOOLS_STAGE,
    WORKSPACE_STAGE,
    package_reference,
)

if TYPE_CHECKING:  # pragma: no cover - types only
    # Imported for the annotations alone. The registry client is reached
    # at call time (see `_package_registry`), and importing it here for a
    # type would put it in the import path of every build.
    from mcuhome.workbench.packageregistry import RegistrySettings, RegistrySource

__all__ = [
    "BUILD_MODES",
    "BUILD_TARGETS",
    "DEFAULT_BUILD_MODE",
    "DEFAULT_BUILD_TARGET",
    "DEFAULT_MAX_WAIT_SECONDS",
    "MODE_CONTAINER",
    "MODE_SUBPROCESS",
    "TARGET_LOCAL",
    "TARGET_REMOTE",
    "BuildOptions",
    "BuildResult",
    "BuildRequest",
    "BuildTarget",
    "ContainerExecution",
    "Execution",
    "LocalBuild",
    "RemoteBuild",
    "RemoteNotConfigured",
    "SubprocessExecution",
    "UnknownBuildMode",
    "UnknownBuildTarget",
    "build_firmware",
    "resolve_build_options",
    "compose_container_build",
    "compose_subprocess_build",
    "image_pin",
    "websocket_url",
    "resolve_build_mode",
    "resolve_build_target",
]

# The two vocabularies above are re-exported, not defined here. They
# live with the classes they name (:mod:`mcuhome.workbench.buildtarget`)
# so that reading a configuration file does not pull in the module that
# dispatches builds:
#
# * `TARGET_LOCAL`/`TARGET_REMOTE` (`BUILD_TARGETS`, `DEFAULT_BUILD_TARGET`)
#   are the values of `build.target` — where a build runs. The default is
#   this machine, because a build server is never discovered.
# * `MODE_CONTAINER`/`MODE_SUBPROCESS` (`BUILD_MODES`,
#   `DEFAULT_BUILD_MODE`) are the values of `build.mode` — how a local
#   build is executed. The default is the container: it needs a container
#   runtime and nothing else of a toolchain, and it is the only mode that
#   isolates a build context, which is untrusted input because it carries
#   patches.

LineSink = Callable[[str], None]


class UnknownBuildTarget(BuildError):
    """A build target by a name that is not one of :data:`BUILD_TARGETS`."""


class UnknownBuildMode(BuildError):
    """A build mode by a name that is not one of :data:`BUILD_MODES`."""


#: The port a build server listens on unless its operator moved it
#: (``mcuhome-buildserver --port``). An address without one means "the
#: usual place", which is what makes `--build-server attic` a complete
#: answer.
DEFAULT_SERVER_PORT = 8100

#: Where the session protocol lives on a build server. One endpoint, so
#: an address never has to carry a path.
SERVER_ENDPOINT = "/ws"


def websocket_url(address: str) -> str:
    """A build server's address, as the URL a client connects to.

    A builder carries a "server address (IP/hostname[:port])" and that is
    what a person types — but what the socket needs is a full WebSocket
    URL, and nothing bridged the two: the address travelled verbatim into
    the connect call, where ``attic:8137`` reads as a URL whose *scheme*
    is ``attic`` and fails with a traceback instead of a refusal.

    Accepted, because all four are things an operator will reasonably
    write down: a bare host, ``host:port``, and either spelling with a
    scheme (``ws``/``wss``, or ``http``/``https`` — which is what a
    browser address bar hands you, and which differ from the first pair
    in nothing but the name). Anything else is refused by name rather
    than by traceback.
    """
    stated = (address or "").strip()
    scheme, separator, rest = stated.partition("://")
    if separator and not scheme:
        scheme, rest = "ws", ""  # "://attic" — the host refusal below is the honest one
    elif not separator:
        # No scheme at all. Deliberately not decided by a URL parser:
        # ``urlsplit("attic:8137")`` reads ``attic`` as the scheme,
        # because a scheme is any word followed by a colon.
        scheme, rest = "ws", stated
    elif scheme in ("http", "https"):
        scheme = "ws" if scheme == "http" else "wss"
    elif scheme not in ("ws", "wss"):
        raise RemoteNotConfigured(
            f'"{stated}" is not a build server address: {scheme} is not one of the '
            f"schemes a build server speaks.",
            hint=(
                "write the address as <host> or <host:port> — a scheme is optional, "
                "and only ws://, wss://, http:// and https:// are understood"
            ),
        )
    host, _, path = rest.partition("/")
    if not host:
        raise RemoteNotConfigured(
            f'"{stated}" names no build server host.',
            hint="write the address as <host> or <host:port>, for example attic:8100",
        )
    if ":" not in host.rpartition("]")[2]:  # a bare host, or a bracketed IPv6 one
        host = f"{host}:{DEFAULT_SERVER_PORT}"
    return f"{scheme}://{host}{SERVER_ENDPOINT if not path else '/' + path}"


class RemoteNotConfigured(BuildError):
    """``remote`` was selected and something it cannot invent is missing.

    Two shapes, and they are the two decisions this target cannot make
    for a caller: **where** to build — there is no default build server
    and no discovery — and **which SDK package** the context pins, which
    is resolved from the caller's own sources and is part of the
    identity the work is attributed to. Both refusals name the knob that
    supplies the value rather than guessing at one, because a guess here
    is either a context sent to a stranger or an identity that describes
    a build nobody asked for.
    """


def resolve_build_target(name: str | None) -> str:
    """The build target *name* selects, or a refusal listing the real ones.

    ``None`` and the empty string mean "no preference" and resolve to
    :data:`DEFAULT_BUILD_TARGET`, so a caller can hand through whatever
    its own configuration ladder produced without checking it first.
    """
    if not name:
        return DEFAULT_BUILD_TARGET
    if name in BUILD_TARGETS:
        return name
    raise UnknownBuildTarget(
        f'"{name}" is not a build target MCUHome knows.',
        hint=(
            "the build targets are "
            + ", ".join(BUILD_TARGETS)
            + f": {TARGET_LOCAL} compiles in a build environment on this "
            f"machine, and {TARGET_REMOTE} on a build server"
        ),
    )


def resolve_build_mode(name: str | None) -> str:
    """The build mode *name* selects, or a refusal listing the real ones.

    ``None`` and the empty string mean "no preference" and resolve to
    :data:`DEFAULT_BUILD_MODE`, so a caller can hand through whatever its
    own configuration ladder produced without checking it first — the
    same contract :func:`resolve_build_target` has for the axis beside
    this one.
    """
    if not name:
        return DEFAULT_BUILD_MODE
    if name in BUILD_MODES:
        return name
    raise UnknownBuildMode(
        f'"{name}" is not a build mode MCUHome knows.',
        hint=(
            "the build modes are "
            + ", ".join(BUILD_MODES)
            + f": {MODE_CONTAINER} compiles in a build container, and "
            f"{MODE_SUBPROCESS} in a build environment unpacked on this machine"
        ),
    )


@dataclass(frozen=True)
class BuildOptions:
    """What the ``build`` section of the configuration says, resolved once.

    Everything in here is a property of **this machine**: where a build
    of it runs, which of the two executions it uses, where it keeps
    unpacked build environments,
    which interpreter creates their virtual environments, how much a
    package may unpack to, where its compiler cache tiers are. None of it
    describes the firmware, which is why none of it lives in a device and
    all of it is configuration.

    It travels as one object rather than as a dozen fields on
    :class:`BuildRequest` for two reasons. A caller that has resolved the
    configuration hands over what it resolved
    (:func:`resolve_build_options`), and a caller that has not — an embedder
    driving a bare model — gets the machine's own answer without having
    to know that these keys exist (:func:`options_for`). And the build
    compositions take one parameter instead of growing one per key.

    Unset values are ``None`` throughout and mean *the default of
    whatever consumes them*, never a value invented here: the store
    resolves its own location from the user's cache home and the cache
    tiers fall back to the cache root, so an option nobody set changes
    nothing. A source list is the exception that proves it — unset means
    *no* operator directory for that package kind, and the package is
    then resolved through the registry rather than under another kind's
    key.
    """

    #: ``build.target``: ``local`` or ``remote`` — where a build of this
    #: machine runs when nothing more explicit said otherwise. A caller
    #: that selects a target per build (a command line's flag, a
    #: configured builder) states it and never reads this.
    target: str = DEFAULT_BUILD_TARGET
    #: ``build.mode``: ``container`` or ``subprocess``.
    mode: str = DEFAULT_BUILD_MODE
    #: ``build.builder``: the named builder a plain build runs at when
    #: nothing was selected per build. Empty is the fallback — no
    #: builder, the target ``build.target`` names.
    builder: str = ""
    #: ``build.container_repositories``: where a container build may take
    #: its environment from, in search order. The image is chosen by the
    #: package set its labels declare, so this list says whose images may
    #: deliver one and never which image is used.
    container_repositories: tuple[str, ...] = DEFAULT_CONTAINER_REPOSITORIES
    #: ``build.cpus`` / ``build.memory``: what one build may use of this
    #: machine. ``None`` is the machine as it is. Both travel into the
    #: request document as the recommendation the environment sizes
    #: itself from, and the container profile enforces them besides.
    cpus: float | None = None
    memory: str | None = None
    #: ``build.env_store``: the store's root. ``None`` is the user's
    #: cache home, which is where a machine nobody configured keeps it.
    env_store: Path | None = None
    #: ``build.dev_workspace``: a west workspace the developer maintains,
    #: built against instead of the environment MCUHome provisions.
    dev_workspace: Path | None = None
    #: ``build.container_program``: the program a container build drives.
    container_program: str = DEFAULT_CONTAINER_PROGRAM
    #: ``build.python``: the interpreter that creates a build
    #: environment's virtual environment. ``None`` is the one MCUHome
    #: itself runs on, which is right whenever the host's Python is the
    #: one the tools package was built for.
    python: str | None = None
    #: ``build.sdk_sources`` / ``build.workspace_sources`` /
    #: ``build.tools_sources``: the operator directories of one package
    #: kind each, and of no other. A kind is never looked for under
    #: another kind's key, so empty means "no operator directory for this
    #: one" and the package is resolved through the registry; a machine
    #: that keeps all three in one place names it in all three keys.
    sdk_sources: tuple[Path, ...] = ()
    workspace_sources: tuple[Path, ...] = ()
    tools_sources: tuple[Path, ...] = ()
    #: ``build.<kind>_max_bytes``: how much each package may unpack to.
    sdk_max_bytes: int | None = None
    workspace_max_bytes: int | None = None
    tools_max_bytes: int | None = None
    #: ``build.cache_root``: where this machine keeps its compiler
    #: cache. ``None`` is the user's cache directory, which is where a
    #: machine nobody configured keeps it.
    cache_root: Path | None = None
    #: ``build.cache_*``: the compiler cache tiers. ``cache_local`` and
    #: ``cache_shared`` name a tier outright; unset, both are laid out
    #: under the cache root the build already resolves.
    cache_local: Path | None = None
    cache_shared: Path | None = None
    cache_session: Path | None = None
    cache_project: Path | None = None
    #: Where each of the values above came from, by the key's leaf name
    #: and in the words :class:`…configuration.Setting` uses: the file,
    #: the variable, the flag, the program, or ``default``. Every key can
    #: say it, so a refusal caused by a configured value can name who
    #: chose it whichever value that was.
    sources: Mapping[str, str] = field(default_factory=dict)

    def source(self, leaf: str) -> str:
        """Where the value of ``build.<leaf>`` came from.

        ``default`` for a key nobody set and for an object nobody
        resolved through the configuration — a caller that built these
        options itself states values and no origins.
        """
        return self.sources.get(leaf, "default")

    def limits(self) -> BuildLimits:
        """What a build of this machine is given, as the two documents
        and the container flags all state it."""
        return resolve_host_limits(cpus=self.cpus, memory_bytes=parse_memory(self.memory))

    def bound(self, kind: str) -> int | None:
        """The configured unpacking bound for a package *kind*, if any."""
        return {
            buildenvstore.KIND_SDK: self.sdk_max_bytes,
            buildenvstore.KIND_WORKSPACE: self.workspace_max_bytes,
            buildenvstore.KIND_TOOLS: self.tools_max_bytes,
        }.get(kind)


def resolve_build_options(settings: Settings) -> BuildOptions:
    """The ``build`` section of a resolved configuration, as one object.

    Every value comes out of the registry that declared it — this
    function knows the names and nothing else, so a key's kind, default,
    validation and the five layers it merged through are stated in
    exactly one place (:data:`mcuhome.workbench.configuration.OPTIONS`).

    Every key of the section is here, including the three package source
    lists: both targets read them, because a remote build resolves its
    pins on this machine before it sends anything.
    """

    def path(name: str) -> Path | None:
        value = settings.value(name)
        return None if value is None else Path(value)

    def number(name: str) -> int | None:
        # A bound the registry answered with its own default is not a
        # statement: the store's table is the same value, and passing it
        # on would make every kind look configured.
        return int(settings.value(name)) if settings.origin(name) != "default" else None

    return BuildOptions(
        target=resolve_build_target(settings.value("build.target")),
        mode=resolve_build_mode(settings.value("build.mode")),
        builder=settings.value("build.builder") or "",
        container_repositories=tuple(settings.value("build.container_repositories")),
        cpus=settings.value("build.cpus"),
        memory=settings.value("build.memory") or None,
        env_store=path("build.env_store"),
        dev_workspace=path("build.dev_workspace"),
        container_program=settings.value("build.container_program"),
        python=settings.value("build.python") or None,
        sdk_sources=tuple(settings.value("build.sdk_sources")),
        workspace_sources=tuple(settings.value("build.workspace_sources")),
        tools_sources=tuple(settings.value("build.tools_sources")),
        sdk_max_bytes=number("build.sdk_max_bytes"),
        workspace_max_bytes=number("build.workspace_max_bytes"),
        tools_max_bytes=number("build.tools_max_bytes"),
        cache_root=path("build.cache_root"),
        cache_local=path("build.cache_local"),
        cache_shared=path("build.cache_shared"),
        cache_session=path("build.cache_session"),
        cache_project=path("build.cache_project"),
        sources={
            entry.name: _stated(settings.setting(f"build.{entry.name}"))
            for entry in fields(BuildOptions)
            if entry.name != "sources" and f"build.{entry.name}" in settings
        },
    )


def _stated(setting: Setting) -> str:
    """Where one resolved value came from, in one word or one path.

    The file, the variable or the flag where there is one, and the layer
    itself where there is not — which is what ``default`` and the
    program layer look like.
    """
    return setting.source or setting.origin


def options_for(request: BuildRequest) -> BuildOptions:
    """The build options of *request* — its own, or this machine's.

    A caller that resolved the configuration itself states the result
    (:attr:`BuildRequest.options`) and is answered with it unchanged. A
    caller that did not gets the configuration resolved here, from the
    environment the request states and the project it names: the system
    and user files, the project's ``mcuhome.yaml``, and the ``MCUHOME_*``
    variables. Not the command line — these options have no flags — so
    the four layers below it are the whole ladder.

    The project is taken as a directory and not read as a project: what
    is wanted from it is one configuration file, and a build is not the
    moment to refuse over a project marker somebody else's tool already
    accepted.
    """
    if request.options is not None:
        return request.options
    project = (
        None
        if request.project_root is None
        else Project(root=Path(request.project_root), discovered=True)
    )
    return resolve_build_options(resolve_settings(project=project, env=request.env))


@dataclass(frozen=True)
class BuildRequest:
    """Everything a build may be given, whichever target runs it.

    :attr:`model` and :attr:`out_dir` are the two every build needs: what
    to build, and the durable directory the unsigned artifacts and the
    build report end up beside. Everything below them is optional and
    named for the target it serves; a field a target does not use is
    ignored rather than refused, because a caller assembling one request
    for a target chosen at run time should not have to assemble two.

    Where a build runs is one field: :attr:`builder` is the destination
    that was selected — the target, the build server behind it, its
    token, the image it delivers — and it is what
    :func:`mcuhome.workbench.configuration.resolve_builder` answers. A
    caller that builds a target object itself
    (:mod:`mcuhome.workbench.buildtarget`) states those values there and
    what it leaves here is ignored.
    """

    #: The canonical device model, stages 1-3 already run.
    model: DeviceModel
    #: The build directory: where a user looks afterwards, and where the
    #: shared signing step reads the report from.
    out_dir: Path
    #: The environment to resolve tools, images and caches from — stated,
    #: never read from the process (:mod:`mcuhome.model.userpaths`).
    env: Mapping[str, str] = field(default_factory=dict)
    #: What the ``build`` section of this machine's configuration says
    #: (:class:`BuildOptions`). ``None`` — the ordinary case — resolves
    #: it here, from :attr:`env` and :attr:`project_root`; a caller that
    #: has already resolved the configuration states the result and is
    #: answered with exactly that.
    options: BuildOptions | None = None
    #: Where this build is to run: the selected build destination, with
    #: its target, its build server and token, and the container image
    #: that machine delivers. ``None`` is the plain build — the target
    #: ``build.target`` names, with every default.
    #:
    #: A builder's ``container_image`` is a statement about the machine
    #: that builds rather than about this build, so a build that starts
    #: no container does not refuse over it: the log says the pin has no
    #: effect here and the build goes on. :attr:`container_image` beats
    #: it wherever a container does run — the more explicit statement
    #: wins.
    builder: SelectedBuilder | None = None
    #: How this machine executes the build: ``container`` or
    #: ``subprocess``, the two values of ``build.mode``. ``None`` takes
    #: that configuration key, which is where the answer ordinarily
    #: comes from; a caller that builds a
    #: :class:`~mcuhome.workbench.buildtarget.BuildTarget` itself states
    #: the execution instead, and one that states a mode here overrides
    #: the configuration for this build.
    mode: str | None = None
    #: The build environment this one build asks for, in the four pin
    #: forms: a repository, ``:tag``, ``@sha256:…``, or a repository with
    #: either. The one-invocation override of the device's own
    #: ``sources.container_image``, and it means the same thing at both
    #: targets — pin this one instead: a local container build resolves
    #: it against the configured repositories, and a remote build sends
    #: it to the server, which resolves it against what it allows.
    #: ``None`` — the ordinary case — leaves the choice to the search,
    #: which accepts an image by the packages its labels declare. Naming
    #: one for a build that starts no container is refused rather than
    #: half-honoured — a statement about *this* build cannot be quietly
    #: dropped.
    container_image: str | None = None
    #: The project directory, which is where the trust anchors are:
    #: ``secrets/trust-anchor/<base-domain>.json``, written when the
    #: project was created. Left ``None`` the build has no project to
    #: read them from and therefore no registry — it then builds from the
    #: configured package directories alone, which is exactly what an
    #: embedder driving a bare model wants.
    project_root: Path | None = None
    #: What the project says about package registries, resolved from
    #: configuration (the ``registry`` option): mirror overrides per
    #: source, and which registries are marked untrusted. Empty means the
    #: defaults — the official registry, its own mirror list, verified.
    registries: Sequence[RegistrySettings] = ()
    #: PEM of the user's MCUboot **public** key. Becomes
    #: ``keys/signing.pub`` in the build context, which is all of the key
    #: pair a build ever sees.
    signing_pub: str = ""
    #: Patches to carry into the build context, laid out as
    #: ``<layer>/NNNN-name.patch``.
    #:
    #: Carried and **not yet read**: the context writer does not take
    #: them yet, so a request that states this field builds exactly the
    #: context it would have built without it. It is stated here because
    #: it is a field of the request in the surface this package is being
    #: brought to, and a field that appeared later would be a second
    #: shape of the same request.
    patches_dir: Path | None = None
    #: A build context directory to build instead of creating one. For a
    #: caller that already holds one — an embedder that assembled a
    #: context elsewhere, a build server that received one over a socket,
    #: a test driving a hand-written one. Left ``None``, which is the
    #: ordinary case, the build creates its own from :attr:`model` and
    #: the configured package directories. Either way it is a *base*
    #: context: locking it is the act of whoever builds it, and a client
    #: that sent one checks the identity the server answers with.
    context_dir: Path | None = None
    #: Scratch area a build may own. Defaults to a hidden directory
    #: under :attr:`out_dir`, which is what a command line wants: rebuilt
    #: every run, thrown away with the build directory.
    work_root: Path | None = None
    #: Wait when the build server has no room. A busy server hands out a
    #: turn instead of a session, and waiting for it is what a person
    #: starting a build almost always wants; ``False`` is the caller that
    #: would rather be told now.
    wait_for_turn: bool = True
    #: How long that wait may last in total, in seconds. ``0`` removes
    #: the bound.
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS
    #: Where the build log goes, line by line, while it happens.
    on_line: LineSink | None = None
    #: Called with a step key when the build enters a new step —
    #: ``"context"`` when the build context is being created,
    #: ``"compile"`` when the build environment starts compiling. The
    #: honest-progress seam: a caller renders steps it was told about,
    #: never ones it guessed. Keys are append-only vocabulary; consumers
    #: ignore keys they do not know.
    #:
    #: A step may be reported a second time with keyword **facts** once
    #: it knows something worth stating — which SDK the context pinned,
    #: which image answered the Zephyr requirement. Facts are display
    #: material and append-only in the same way: a consumer renders what
    #: it recognizes and ignores the rest, and a composition that has
    #: nothing to say states nothing rather than inventing it.
    on_step: Callable[..., None] | None = None
    #: Called with a
    #: :class:`~mcuhome.workbench.sessionclient.SeatWait` each time a
    #: turn is refused, so a caller can say something true while nothing
    #: is happening. Deliberately **not** a step: the build has not
    #: started and may never start, and a step bar claiming otherwise
    #: would be showing progress that does not exist.
    on_wait: Callable[[Any], None] | None = None
    #: Asked while the build runs: ``True`` means stop it. The one seam
    #: on this surface that decides control flow, because cancelling the
    #: task awaiting :func:`build_firmware` cancels nothing — the work is
    #: in a worker thread and a container does not care what a loop does.
    #:
    #: A stopped build ends the way a build that ran out of time ends:
    #: the build environment is signalled and then killed, a container is
    #: removed, the build directory is released, :attr:`BuildResult.out_dir`
    #: keeps whatever had been written, and the answer is
    #: :attr:`BuildResult.ok` false with :attr:`BuildResult.stopped`
    #: true. At the remote target the build server is told — the session
    #: protocol's ``cancel`` — and a server that stays silent is taken as
    #: stopped once the ladder's bound has passed
    #: (:func:`~mcuhome.workbench.buildprocess.resolve_shutdown_seconds`).
    #:
    #: **Where it is asked**: while the build environment runs, on the
    #: supervisor's half-second tick, and at the remote target while the
    #: build waits for a turn. What comes before that — creating the
    #: context, fetching an environment, uploading it to a build server —
    #: runs to its end; those are bounded by what they move rather than
    #: by a caller's patience, and a build stopped halfway through them
    #: would leave a half-written environment behind that the next build
    #: would have to distrust.
    #:
    #: **On which thread**: for a local build in the worker thread the
    #: build runs in (``asyncio.to_thread``), so a predicate that touches
    #: a caller's own state has to be safe to call from there and must
    #: not assume an event loop; for the remote target on the thread
    #: running :func:`build_firmware` itself. It is asked about twice a
    #: second, so it answers rather than computes.
    #:
    #: **A build that finished is not a stopped build.** A stop that
    #: arrives while the last step is already succeeding answers
    #: ``ok=True, stopped=False``: the firmware exists, and telling a
    #: caller its build was stopped would throw away what it asked for.
    should_stop: Callable[[], bool] | None = None


@dataclass(frozen=True)
class BuildResult:
    """What a build produced, in the one shape every build answers.

    :attr:`ok` is the verdict and the only field a caller must consult
    before the others mean anything; :attr:`stopped` says which kind of
    "no" it was, a build that failed or one somebody stopped.
    :attr:`out_dir` is where the **unsigned** artifacts and the build
    report are, and :attr:`report` is that report's file name — the two
    together are what the one shared signing step needs, and they are
    the whole reason this class exists.

    There is no status beside the verdict: a firmware build either
    produced the artifacts or it did not. A build environment that
    answers ``unsupported`` to the build action is an *unusable*
    environment rather than a failed build, and the compositions raise
    :class:`~mcuhome.workbench.buildenvsession.EnvironmentUnusable` for
    it — a caller looking for another environment needs a refusal, not a
    third word in a document.

    :attr:`artifacts` is the declared artifact set, in
    :class:`~mcuhome.model.artifacts.Artifact` — the same type whichever
    target produced it.
    """

    #: Whether this build produced what it was asked for.
    ok: bool
    #: Which of :data:`BUILD_TARGETS` ran.
    target: str
    #: The device that was built, by its own name.
    device: str
    #: The identity the work is attributed to: the build context's ID.
    context_id: str
    artifacts: tuple[Artifact, ...]
    #: Where the unsigned artifacts and the report are.
    out_dir: Path | None
    #: The build report's file name in :attr:`out_dir`:
    #: ``build-report.json``, which carries the imgtool parameters the
    #: host signer needs.
    report: str
    #: The build environment that ran, where one did: the image pinned
    #: to the digest whose labels were checked, whether this machine
    #: started it or a build server did. Empty for a build that used no
    #: image at all — the subprocess profile — and for a server that
    #: named none.
    container_image: str = ""
    #: Whether this build was stopped rather than finished: it did not
    #: produce what it was asked for **because** somebody ended it. A
    #: stopped build is not a failed one, and a caller that renders the
    #: two the same way tells a person their firmware is broken when they
    #: pressed the stop button. Never true beside :attr:`ok`: a build
    #: whose last step succeeded while the stop was arriving produced the
    #: firmware, and that is the answer its caller wanted.
    stopped: bool = False
    #: The composition's own result object, untouched. Useful for
    #: logging and never part of a document: what is in it depends on
    #: which composition ran, which is the one thing this class exists to
    #: hide.
    detail: Any = None

    def to_dict(self) -> dict[str, Any]:
        """This build as a document, JSON-ready and complete.

        Every key is always present: a client that renders a build
        should not have to ask whether a missing key means "nothing" or
        "this version did not know about it". :attr:`detail` is not in
        here, because no two compositions would put the same thing in it.
        """
        return {
            "ok": self.ok,
            "stopped": self.stopped,
            "target": self.target,
            "device": self.device,
            "context_id": self.context_id,
            "out_dir": None if self.out_dir is None else str(self.out_dir),
            "report": self.report,
            "container_image": self.container_image,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


#: What to do about an environment that does not implement a build, per
#: the thing that ran it — because who can replace that environment
#: differs: a container build picks its image, a subprocess build runs
#: what is unpacked in the store, and a remote build runs what somebody
#: else's operator provisioned.
_UNSUPPORTED_HINTS = {
    MODE_CONTAINER: (
        "the image that delivered it does not implement this build, and no retry "
        "changes that. Build without naming an image, so MCUHome searches the "
        "configured repositories for one that does:\n"
        "    mcuhome device build <device>\n"
        "(drop --container-image, and `sources.container_image` in the device file "
        "if it names one)"
    ),
    MODE_SUBPROCESS: (
        "the build environment unpacked on this machine does not implement this "
        "build, and no retry changes that. Build in a container, where MCUHome "
        "delivers the environment this device's context names:\n"
        "    mcuhome config set build.mode container"
    ),
    TARGET_REMOTE: (
        "the build server chose that environment out of what its operator "
        "provisioned, so this side cannot replace it. Ask the operator for an "
        "environment that implements this build, or build on this machine:\n"
        "    mcuhome device build <device> --build-target local"
    ),
}


class _StopSwitch:
    """The caller's stop predicate, and whether it ever said yes.

    A build that ended has to say **which** kind of no it was, and the
    only side that can say is the one that was asked: a failed compile
    and a build somebody stopped look identical from the artifacts, and
    a client that renders them the same way tells a person their
    firmware is broken when they pressed the stop button.

    **Latched**, so that the answer cannot change after the fact: a
    predicate reading a flag somebody else clears would otherwise leave
    a build that was stopped calling itself failed. Once it has said
    stop, this says stop — a build is not un-stopped.

    A caller that supplied nothing is the switch that is never on and is
    never asked.
    """

    def __init__(self, predicate: Callable[[], bool] | None) -> None:
        self._predicate = predicate
        self.stopped = False

    def __call__(self) -> bool:
        if self.stopped:
            return True
        if self._predicate is None:
            return False
        self.stopped = bool(self._predicate())
        return self.stopped

    @property
    def armed(self) -> Callable[[], bool] | None:
        """This switch where there is a predicate behind it, else ``None``.

        Handed down instead of the caller's own predicate, so that the
        composition asks *through* the latch; ``None`` where there is
        nothing to ask, so that nothing below has to tell an idle
        predicate from a missing one.
        """
        return None if self._predicate is None else self


def _refuse_unsupported(status: str, *, ran: str) -> None:
    """A build environment that cannot do a build at all is unusable.

    ``unsupported`` is the specification's word for *no environment of
    my kind can do this*, and it says nothing about the firmware: the
    environment does not implement the action, or not in the generation
    it was asked in. Carrying that into the result as a third verdict
    would tell a caller its build failed, when what it has to do is find
    another environment.

    *ran* is what ran it — a build mode, or ``remote`` — because that is
    what decides whose environment it was and therefore what the person
    reading the refusal can actually do about it.
    """
    if status != STATUS_UNSUPPORTED:
        return
    raise EnvironmentUnusable(
        "This build environment cannot run a firmware build.",
        hint=_UNSUPPORTED_HINTS[ran],
    )


def _reported(limits: BuildLimits) -> dict[str, Any]:
    """What the ``compile`` step says about the budget it hands over.

    Facts a consumer can render, and the ones a person recognizes from
    the flags: how many cores and how much memory this build may use.
    The parallelism itself is not among them — the build environment
    derives that from these two, and this side would be guessing at it.
    """
    return {"cpus": limits.cpus, "memory_bytes": limits.memory_bytes}


def _work_root(request: BuildRequest, name: str) -> Path:
    return Path(request.work_root) if request.work_root else Path(request.out_dir) / name


def _into_the_log(on_line: LineSink | None) -> Callable[[Diagnostic], None] | None:
    """A warning channel that writes into the build log.

    A build has no second stream for findings: what it learns while it
    runs belongs where the person watching it is looking. The message is
    what goes in — the rest of the finding is for a client that renders
    documents, and the log is text.
    """
    if on_line is None:
        return None

    def report(finding: Diagnostic) -> None:
        on_line(finding.message)

    return report


def _package_registry(
    model: DeviceModel,
    *,
    project_root: Path | None,
    registries: Sequence[RegistrySettings],
    work_root: Path,
    on_line: LineSink | None,
) -> RegistrySource | None:
    """The registry this device's SDK would come from, promised not built.

    Which registry is the device's own statement: ``sources.sdk`` is a
    reference, and the registry is the domain in it — the official one
    where it names none. Nothing here reads a trust anchor or opens a
    socket; :func:`~mcuhome.workbench.packageregistry.open_package_registry`
    defers all of it to the first question actually asked, so a build
    whose packages are already in the operator's directories neither
    needs an anchor nor is stopped by a missing one.

    Without a *project_root* there is no ``secrets/trust-anchor/`` to
    read and therefore no registry — an embedder driving a bare model
    builds from its own source directories, which is what it asked for.

    The warning channel is the build log, deliberately: an unverified
    registry has to say so where the person watching the build is
    looking, not in a stream nobody attached to.
    """
    if project_root is None:
        return None
    from mcuhome.workbench.packageregistry import open_package_registry

    # Read by the resolver's own parser rather than the image one: a
    # `sources.sdk` may carry a version *constraint* where an image
    # reference carries a tag, and `~=0.1.9` is not a tag.
    reference = package_reference(model.sources.sdk, stage=SDK_STAGE)
    return open_package_registry(
        reference.base_domain,
        project_root=Path(project_root),
        settings=tuple(registries),
        into=Path(work_root) / "registry",
        on_warning=_into_the_log(on_line),
    )


def _package_hosts(
    *,
    project_root: Path | None,
    registries: Sequence[RegistrySettings],
    work_root: Path,
    on_line: LineSink | None,
) -> Callable[[str], Any] | None:
    """A registry per base domain, for a device that points one package elsewhere.

    The SDK's host is what :func:`_package_registry` opens and what
    almost every build reads. A ``sources.*`` reference may name another
    one, and that registry has its own trust anchor and its own mirrors —
    so the resolution is handed a way to open one per domain rather than
    one client. Built lazily per domain: a build that never names a
    second host never reads a second anchor.
    """
    if project_root is None:
        return None
    from mcuhome.workbench.packageregistry import registry_opener

    return registry_opener(
        project_root=Path(project_root),
        settings=tuple(registries),
        into=Path(work_root) / "registry",
        on_warning=_into_the_log(on_line),
    )


def _refuse_image_without_container(container_image: str, *, source: str) -> ConfigError:
    """A build container was named for a build that does not start one.

    The two statements contradict each other and neither can be
    honoured halfway: ignoring the image would compile against something
    other than what was named, and ignoring the mode would start a
    container the machine is configured not to use. So the build stops
    before anything is fetched, and says which of the two to drop —
    *source* names where the mode came from, because it usually came
    from a file the person is not looking at.
    """
    return ConfigError(
        f"This build was given the container image {container_image}, and it is set to "
        f"build without a container.",
        hint=(
            f"the build mode is {MODE_SUBPROCESS} (from {source}). "
            f"Either drop the image, or build in a container:\n"
            f"    mcuhome config set build.mode {MODE_CONTAINER}\n"
            f"A build without a container runs the build environment MCUHome unpacked "
            f"on this machine, which no image reference can name."
        ),
    )


def _refuse_developer_without_subprocess(workspace: Path, *, source: str) -> ConfigError:
    """A development workspace was named for a build that runs in a container.

    The two cannot be combined, and not for want of plumbing: a
    development build runs the builder out of the workspace's own SDK
    checkout with the tools on the person's ``PATH``, and a container
    build runs a fixed image that has neither. Mounting the workspace
    into the image would build it with the image's toolchain, which is
    not what the setting asks for and not something anybody could tell
    from the result.
    """
    return ConfigError(
        f"This build is set to compile in a build container and to use the development "
        f"workspace {workspace}.",
        hint=(
            f"the build mode is {MODE_CONTAINER} (from {source}). A development build "
            f"runs on this machine with your own tools, so either build without a "
            f"container:\n"
            f"    mcuhome config set build.mode {MODE_SUBPROCESS}\n"
            f"or unset {subprocessbuild.DEV_WORKSPACE_OPTION} to build in the container."
        ),
    )


def _refuse_developer_remotely(workspace: Path) -> ConfigError:
    """A development workspace was named for a build that happens elsewhere.

    A development build compiles the workspace on this machine with the
    tools on this ``PATH``; a build server has neither, and the context
    such a build writes names no environment a server could resolve. So
    the setting cannot be honoured there and cannot be quietly dropped
    either — a build that ignored it would compile the pinned packages
    and look exactly like the build the person meant.
    """
    return ConfigError(
        f"This build is set to run on a build server and to use the development "
        f"workspace {workspace}.",
        hint=(
            "a development build compiles that workspace here, with your own tools, so "
            f"either build locally:\n"
            f"    mcuhome device build <device> --build-target {TARGET_LOCAL}\n"
            f"or unset {subprocessbuild.DEV_WORKSPACE_OPTION} to build on the server."
        ),
    )


def _note_image_without_container(
    model: DeviceModel | None, stated_container_image: str | None = None, *, on_line: Any = None
) -> None:
    """Say once that an image named for this build does not apply to it.

    Two statements can name one and neither is wrong: the device's
    ``sources.container_image`` travels with the device and is about the
    delivery it gets on a machine that builds in a container, and a
    configured builder's ``container_image:`` is about that machine rather than
    about this build. A build without a container has no image for
    either of them to name. So this is neither a refusal nor silence:
    the build log carries one line, where the person watching the build
    is already looking.

    Whichever is the more specific statement is the one named, because
    it is also the one that would have won if a container had run.

    *model* is ``None`` where the device's own pin must not be spoken
    about — a development build is refused over it a moment later, and a
    note saying it has no effect would say the opposite of what happens
    next. A builder's image is still noted there, because nothing else
    ever mentions it.
    """
    stated = stated_container_image or (
        model.sources.container_image if model is not None else None
    )
    if not stated or on_line is None:
        return
    on_line(
        f"Note: this build was given the container image {stated}, and it runs "
        f"without a container — the image has no effect here."
    )


def _stated_container_image(request: BuildRequest) -> str | None:
    """The image named for this build, the more specific statement first.

    :attr:`BuildRequest.container_image` is about this one invocation and
    a builder's own ``container_image`` is about the machine it builds
    at, so the first beats the second wherever a container actually runs.
    Neither is the device's own pin: that one is read where the device is
    (:func:`image_pin`), because it survives this invocation.
    """
    if request.container_image is not None:
        return request.container_image
    return None if request.builder is None else request.builder.container_image


def image_pin(model: DeviceModel, override: str | None) -> str | None:
    """The build environment this build asks for, override first.

    Two statements can name one, and the more specific of the two wins:
    *override* is what this invocation asked for and
    ``sources.container_image`` is what the device carries from build to
    build. Neither is a requirement on the image beyond its name — the
    package set the context pinned is checked against the labels of
    whatever the pin resolves to, so a pin narrows the search and never
    what is accepted.

    ``None`` from both is the ordinary case and means "find one": the
    configured repositories are searched for an image whose labels
    declare exactly that package set.
    """
    if override is not None:
        return override
    return model.sources.container_image or None


def build_target_for(name: str | None, request: BuildRequest) -> BuildTarget:
    """The build target a target *name* and a request describe together.

    The bridge between the word and the object: ``local`` and ``remote``
    are what a flag and a configuration key carry, and
    :mod:`mcuhome.workbench.buildtarget` states what each of them is made
    of. Every target-specific field of :class:`BuildRequest` is read
    **here and nowhere else**, so that the day those fields move onto the
    targets there is one call site to change rather than two
    compositions.

    *name* goes through :func:`resolve_build_target` first, so an unknown
    one is refused by name. ``None`` and the empty string mean "no
    preference": the selected builder answers where it has one, and
    ``build.target`` answers otherwise.

    **This is also where the configuration is consulted** for the values
    a request leaves open — the target, the mode and the development
    workspace. What the request states wins over what the machine is
    configured to do: a caller that named a value meant it, and a
    selected builder is such a statement.
    """
    options = options_for(request)
    if name:
        chosen = resolve_build_target(name)
    elif request.builder is not None:
        chosen = resolve_build_target(request.builder.target)
    else:
        chosen = options.target
    if chosen == TARGET_LOCAL:
        mode = resolve_build_mode(request.mode) if request.mode else options.mode
        if mode == MODE_SUBPROCESS:
            if request.container_image is not None:
                raise _refuse_image_without_container(
                    request.container_image,
                    # Whoever chose the mode is who has to be told, and a
                    # mode this request states itself did not come from
                    # any configuration file.
                    source="this build" if request.mode else options.source("mode"),
                )
            return LocalBuild(
                execution=SubprocessExecution(
                    dev_workspace=options.dev_workspace,
                    # Not a refusal: a builder's image says what the
                    # machine delivers, and a machine configured to build
                    # without a container would otherwise refuse every
                    # build it ever runs. The composition notes it.
                    stated_container_image=(
                        None if request.builder is None else request.builder.container_image
                    ),
                )
            )
        developing = options.dev_workspace
        if developing is not None:
            raise _refuse_developer_without_subprocess(
                developing,
                source="this build" if request.mode else options.source("mode"),
            )
        return LocalBuild(
            execution=ContainerExecution(container_image=_stated_container_image(request))
        )
    developing = options.dev_workspace
    if developing is not None:
        raise _refuse_developer_remotely(developing)
    return RemoteBuild(
        server=None if request.builder is None else request.builder.server,
        token=None if request.builder is None else request.builder.token,
        wait=request.wait_for_turn,
        max_wait_seconds=request.max_wait_seconds,
        # The pin reaches the far side as well — this invocation's, or
        # the device's own: a remote build that quietly ignored it would
        # build in an environment other than the one it was told to,
        # which is the one thing an image pin exists to prevent. What is
        # allowed there stays the server operator's decision.
        container_image=image_pin(request.model, _stated_container_image(request)),
    )


async def build_firmware(
    request: BuildRequest, *, target: BuildTarget | str | None = None
) -> BuildResult:
    """Build *request*, wherever *target* says, in the one outcome shape.

    One build, one entry point, whichever target runs it. Above it a
    caller decides *what* to build (a device model, or a build context
    already created from one) and *where*; below it the three
    compositions differ in everything and agree on the answer. It is also
    where a build server enters: what reaches it over a socket is a
    context and a target of its own making, and from that point on the
    work is the same work a local build does.

    *target* is what a caller has: a **target object** for the caller
    that assembled one itself, a **name** — ``local`` or ``remote`` —
    for the caller whose choice arrived as a flag or a configuration
    value, and ``None`` for no preference, which takes the request's
    selected builder and then ``build.target``. A name that is not one
    of :data:`BUILD_TARGETS` is :class:`UnknownBuildTarget`; a target
    object this package does not implement is a :class:`TypeError`,
    because a name can be mistyped and an object cannot.

    Raises whatever typed refusal the target's composition raises — a
    missing build container, a missing SDK package, a build server that
    said no. A build that ran and *failed* is not an exception: it comes
    back with :attr:`BuildResult.ok` false and the composition's own
    account in :attr:`BuildResult.detail`, because a failed compile is an
    answer and a caller usually wants to render it rather than catch it.
    A build environment that answers ``unsupported`` is the exception
    and not an answer at all: it is
    :class:`~mcuhome.workbench.buildenvsession.EnvironmentUnusable`.

    The build directory is held for the duration (:mod:`…buildlock`), so
    a second build of it refuses in words instead of deleting this one's
    work under it. Here rather than in a composition, because every one
    of them writes into the same directory and the collision does not
    care which two were running — nor whether the other one is a command
    line or a dashboard.

    **Stopping is** :attr:`BuildRequest.should_stop` **and not task
    cancellation**: the work of a local build happens in a worker thread
    and a remote one happens on somebody else's machine, so cancelling
    whatever awaits this coroutine leaves both of them running. A build
    the predicate ended comes back with :attr:`BuildResult.ok` false and
    :attr:`BuildResult.stopped` true, having released the build
    directory on the way out like every other answer here — and within a
    bound, wherever it ran
    (:func:`~mcuhome.workbench.buildprocess.resolve_shutdown_seconds`),
    because the directory stays held until it comes back.
    """
    if not isinstance(target, BuildTarget):
        target = build_target_for(target, request)
    with open_build_lock(request.out_dir, device=request.model.device.name):
        if isinstance(target, LocalBuild):
            execution = target.execution
            if isinstance(execution, ContainerExecution):
                return await _run_local(request, execution)
            if isinstance(execution, SubprocessExecution):
                return await _run_subprocess(request, execution)
            raise TypeError(
                f"{type(execution).__name__} is not a build execution this package runs"
            )
        if isinstance(target, RemoteBuild):
            return await _run_remote(request, target)
        raise TypeError(f"{type(target).__name__} is not a build target this package runs")


def compose_local_build(
    model: DeviceModel,
    *,
    signing_pub: str,
    sdk_sources: Sequence[Path],
    work_root: Path,
    env: dict[str, str],
    project_root: Path | None = None,
    registries: Sequence[RegistrySettings] = (),
    container_image: str | None = None,
    cache_root: Path | None = None,
    created: datetime | None = None,
    context_dir: Path | None = None,
    on_line: Any = None,
    on_step: Any = None,
    runtime: Any = None,
    registry: Any = None,
    images: Any = None,
    mode: str = DEFAULT_BUILD_MODE,
    environment: Any = None,
    options: BuildOptions | None = None,
    stated_container_image: str | None = None,
    should_stop: Callable[[], bool] | None = None,
):
    """The local build, dispatched to the execution this machine uses.

    One entry point for the two executions, so that a mode a
    configuration produced reaches the right composition without every
    caller learning both: ``container`` is
    :func:`compose_container_build` and ``subprocess` is
    :func:`compose_subprocess_build`. *environment* — the store entries a
    build runs against — belongs to the second alone, and
    *container_image*, *runtime* and *images* to the first.

    Synchronous, because both compositions are; ``build_firmware``
    offloads them.
    """
    options = options if options is not None else BuildOptions()
    if resolve_build_mode(mode) == MODE_SUBPROCESS:
        return compose_subprocess_build(
            model,
            sdk_sources=sdk_sources,
            work_root=work_root,
            env=env,
            signing_pub=signing_pub,
            project_root=project_root,
            registries=registries,
            environment=environment,
            created=created,
            cache_root=cache_root,
            context_dir=context_dir,
            on_line=on_line,
            on_step=on_step,
            registry=registry,
            options=options,
            stated_container_image=stated_container_image,
            should_stop=should_stop,
        )
    return compose_container_build(
        model,
        signing_pub=signing_pub,
        sdk_sources=sdk_sources,
        work_root=work_root,
        env=env,
        project_root=project_root,
        registries=registries,
        container_image=container_image,
        cache_root=cache_root,
        created=created,
        context_dir=context_dir,
        on_line=on_line,
        on_step=on_step,
        runtime=runtime,
        registry=registry,
        images=images,
        options=options,
        should_stop=should_stop,
    )


def compose_container_build(
    model: DeviceModel,
    *,
    signing_pub: str = "",
    sdk_sources: Sequence[Path],
    work_root: Path,
    env: dict[str, str],
    project_root: Path | None = None,
    registries: Sequence[RegistrySettings] = (),
    container_image: str | None = None,
    cache_root: Path | None = None,
    created: datetime | None = None,
    context_dir: Path | None = None,
    on_line: Any = None,
    on_step: Any = None,
    runtime: Any = None,
    registry: Any = None,
    images: Any = None,
    options: BuildOptions | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> containerbuild.ContainerBuildResult:
    """The container execution's composition: create, resolve, lock, drive.

    The same three announced steps the subprocess execution has, in the
    same order and for the same reason: **context** is the locked
    directory the build is attributed to, **environment** is the image
    that delivers the package set that context pinned
    (:func:`~mcuhome.workbench.containerbuild.prepare_environment` — a
    registry question, and where a gigabyte may be fetched), and
    **compile** hands the locked context to one container per step
    (:func:`~mcuhome.workbench.containerbuild.run_locked_build`).

    **The context comes first, and that is what pinning by packages
    means.** There is nothing to resolve until the context exists,
    because the context is what names the packages the image has to
    declare. A build environment is therefore never chosen from a
    device's wishes, only from what the resolved context pinned.

    *container_image* is the one-invocation override, in any of the four
    pin forms; without one the device's own ``sources.container_image``
    is the pin (:func:`image_pin`). Either narrows which images are looked
    at and never what is accepted.
    *context_dir* is the caller that already holds a **base** context and
    wants this one built — an embedder that assembled one elsewhere, a
    build server that received one over a socket. It is used as it is:
    nothing is written into it but the lock, and no context step is
    announced, because this composition did not create one.

    *runtime*, *registry* and *images* are the three seams a caller may
    replace: the container runtime, the package registry the pins are
    resolved and fetched through (``None`` derives it from the project),
    and the container registry the image labels are read from.

    *should_stop* is handed to the profile, which asks it while the step
    runs; the two steps before it — creating the context and resolving
    the environment — run to their end.
    """
    options = options if options is not None else BuildOptions()
    limits = options.limits()
    pin_wanted = image_pin(model, container_image)
    sources = tuple(Path(source) for source in sdk_sources)
    work_root = Path(work_root)
    packages = (
        registry
        if registry is not None
        else _package_registry(
            model,
            project_root=project_root,
            registries=registries,
            work_root=work_root,
            on_line=on_line,
        )
    )
    supplied = context_dir is not None
    context_dir = Path(context_dir) if supplied else work_root / "context"
    if not supplied:
        if on_step is not None:
            on_step("context")
        create_build_context(
            model,
            out_dir=context_dir,
            work_root=work_root,
            sdk_sources=sources,
            workspace_sources=options.workspace_sources,
            tools_sources=options.tools_sources,
            sdk_max_bytes=options.sdk_max_bytes,
            signing_pub=signing_pub,
            created=created or datetime.now(UTC),
            registry=packages,
            hosts=_package_hosts(
                project_root=project_root,
                registries=registries,
                work_root=work_root,
                on_line=on_line,
            ),
            on_line=on_line,
        )
        if on_step is not None:
            # What the context turned out to be, read back off the
            # directory: the step announced itself before any of this was
            # decided, and the decisions are the interesting part.
            on_step("context", **read_context_facts(context_dir))

    if on_step is not None:
        on_step("environment")
    pin = read_context_request(context_dir / CONTEXT_FILE).build_environment
    resolved = containerbuild.prepare_environment(
        pin,
        env=env,
        repositories=options.container_repositories,
        image_pin=pin_wanted,
        workspace_source=package_reference(
            model.sources.build_workspace, stage=WORKSPACE_STAGE
        ).source,
        tools_source=package_reference(model.sources.build_tools, stage=TOOLS_STAGE).source,
        sources=sources,
        workspace_sources=options.workspace_sources,
        tools_sources=options.tools_sources,
        registry=packages,
        images=images,
        container_program=containerbuild.resolve_container_program(options=options),
        runtime=runtime,
        on_line=on_line,
    )
    # Everything the image declares beyond its packages, against what
    # this build needs: the specification generation it implements, the
    # build contexts it accepts, and the Zephyr release it builds
    # against. The package set itself is what found it. It runs here so
    # that a refusal costs no lock in a directory the user keeps, and
    # again in `run_locked_build`, which is the entry point an embedder
    # and a build server reach directly.
    containerbuild.require_container_image(
        resolved.declaration,
        container_image=resolved.reference,
        generator=format_generator_chain(read_generator_chain(context_dir / BUILD_CONTEXT_FILE)),
        zephyr_constraint=model.toolchain.zephyr_constraint,
    )
    if on_step is not None:
        on_step(
            "environment",
            build_environment=resolved.reference,
            zephyr=resolved.declaration.zephyr_version,
            found_under=resolved.match.found_under,
            fetched=resolved.fetched,
        )
    lock_context(context_dir)
    if on_step is not None:
        on_step("compile", container_image=resolved.reference, **_reported(limits))
    root = containerbuild.resolve_cache_root(
        options=replace(options, cache_root=cache_root or options.cache_root), env=env
    )
    return containerbuild.run_locked_build(
        context_dir,
        container_image=resolved,
        sdk_sources=sources,
        work_root=work_root / "backend",
        env=dict(env),
        limits=limits,
        sdk_max_bytes=options.sdk_max_bytes,
        zephyr_constraint=model.toolchain.zephyr_constraint,
        container_program=containerbuild.resolve_container_program(options=options),
        # The same cache root and the same tiers the subprocess profile
        # is given: one cache per user, laid out once, mounted here and
        # linked there.
        tiers=resolve_cache_tiers(
            cache_root=root,
            local=options.cache_local,
            shared=options.cache_shared,
            session=options.cache_session,
            project=options.cache_project,
        ),
        registry=packages,
        runtime=runtime,
        on_line=on_line,
        should_stop=should_stop,
    )


def compose_subprocess_build(
    model: DeviceModel,
    *,
    sdk_sources: Sequence[Path],
    work_root: Path,
    env: dict[str, str],
    signing_pub: str = "",
    environment: Any = None,
    project_root: Path | None = None,
    registries: Sequence[RegistrySettings] = (),
    created: datetime | None = None,
    cache_root: Path | None = None,
    context_dir: Path | None = None,
    on_line: Any = None,
    on_step: Any = None,
    registry: Any = None,
    options: BuildOptions | None = None,
    stated_container_image: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> subprocessbuild.SubprocessBuildResult:
    """The subprocess execution's composition: environment, lock, drive.

    The same three announced steps a container build has, minus the one
    that fetches an image. **environment** is the package set this build
    runs against, already provisioned into the store and handed over as
    the two frozen entries
    (:class:`mcuhome.workbench.subprocessbuild.Environment`);
    **context** is the locked directory the build is attributed to; and
    **compile** hands the two to
    :func:`mcuhome.workbench.subprocessbuild.run_locked_build`.

    **The context comes first here, and that is the profile's own
    order.** A container build resolves an image and then writes a
    context; this one has nothing to resolve until the context exists,
    because the context is what pins the packages the environment is
    provisioned from. So the steps are announced context, environment,
    compile — the same three names, in the order the decisions actually
    happen in.

    *environment* is the development-mode entrance: a caller that already
    holds two trees hands them over and nothing is provisioned. Left
    ``None``, the store answers — the pinned packages are acquired,
    verified, unpacked and frozen
    (:func:`~mcuhome.workbench.subprocessbuild.environment_from_pins`),
    which is a no-op for anything already there.

    Two refusals happen before the context is locked, because locking
    writes into a directory the user keeps and a build that is going to
    be refused must not have changed anything first: a context that
    carries patches against developer-maintained trees
    (:func:`~mcuhome.workbench.subprocessbuild.refuse_patched_context`),
    and an environment that does not agree with what it is being asked to
    build (:func:`~mcuhome.workbench.subprocessbuild.check_environment` —
    the specification generation it implements, the build contexts it
    accepts, the packages it consists of and the Zephyr release it builds
    against).

    An image named for this build is **not** refused here and not
    honoured either: this build starts no container, so there is no
    image for it to name. A device's ``sources.container_image`` is a
    statement about the delivery the device gets on a machine that does
    start one, and *stated_container_image* — a configured builder's
    ``container_image`` — is a statement about that machine; the packages either would have
    delivered are what this build provisions itself. The log says so
    once rather than leaving the person to wonder
    (:func:`_note_image_without_container`). An image stated for *this
    invocation* is the one that is refused, and it is refused before any
    of this runs (:func:`_refuse_image_without_container`), because that
    statement is about this build and cannot be dropped without changing
    what was asked for.

    A *development* build is the one place the device's pin is refused,
    and the context writer does it with the other ``sources`` entries:
    nothing there is fetched at all — so the note is not printed there,
    because the refusal that follows says the opposite of it.

    *should_stop* is handed to the profile, which asks it while the step
    runs; the two steps before it — creating the context and provisioning
    the environment — run to their end.
    """
    options = options if options is not None else BuildOptions()
    limits = options.limits()
    sources = tuple(Path(source) for source in sdk_sources)
    work_root = Path(work_root)
    packages = _package_registry(
        model,
        project_root=project_root,
        registries=registries,
        work_root=work_root,
        on_line=on_line,
    )
    developing = environment is not None and environment.developer
    # After the development question is settled, because the answer
    # decides whether the device's own pin may be spoken about at all: a
    # development build is refused over that pin a moment later.
    _note_image_without_container(
        None if developing else model, stated_container_image, on_line=on_line
    )
    supplied = context_dir is not None
    context_dir = Path(context_dir) if supplied else work_root / "context"
    if not supplied:
        if on_step is not None:
            on_step("context")
        create_build_context(
            model,
            out_dir=context_dir,
            work_root=work_root,
            sdk_sources=sources,
            workspace_sources=options.workspace_sources,
            tools_sources=options.tools_sources,
            sdk_max_bytes=options.sdk_max_bytes,
            signing_pub=signing_pub,
            created=created or datetime.now(UTC),
            registry=packages,
            hosts=_package_hosts(
                project_root=project_root,
                registries=registries,
                work_root=work_root,
                on_line=on_line,
            ),
            developer=developing,
            on_line=on_line,
        )
        if on_step is not None:
            on_step("context", **read_context_facts(context_dir))

    if environment is not None:
        # A development build's refusal, before this composition has read
        # or written anything else: a context that carries patches cannot
        # be built against a workspace the developer maintains, and the
        # person has to hear that before a lock lands in a directory they
        # keep.
        subprocessbuild.refuse_patched_context(context_dir, environment)
    pin = read_context_request(context_dir / CONTEXT_FILE).build_environment
    if supplied and developing and not isinstance(pin, DeveloperEnvironment):
        # A context somebody else created, pinning an environment, handed
        # to a build that would compile a workspace instead. Building it
        # anyway would produce firmware whose context claims it was
        # compiled from packages it never saw — the one thing a pin is
        # for. Neither half can be honoured, so neither is.
        raise EnvironmentUnavailable(
            f"This build context is pinned to {pin.described()} and this build compiles "
            f"the workspace at {environment.workspace.path}.",
            hint=(
                f"unset {subprocessbuild.DEV_WORKSPACE_OPTION} to build the context as "
                "it is pinned, or let this build create its own context from the device"
            ),
        )
    if on_step is not None:
        on_step("environment")
    if environment is None:
        environment = subprocessbuild.environment_from_pins(
            pin,
            env=dict(env),
            workspace_source=package_reference(
                model.sources.build_workspace, stage=WORKSPACE_STAGE
            ).source,
            tools_source=package_reference(model.sources.build_tools, stage=TOOLS_STAGE).source,
            sources=sources,
            workspace_sources=options.workspace_sources,
            tools_sources=options.tools_sources,
            store=options.env_store,
            interpreter=options.python,
            bounds={
                kind: bound
                for kind in (buildenvstore.KIND_WORKSPACE, buildenvstore.KIND_TOOLS)
                if (bound := options.bound(kind)) is not None
            },
            registry=packages,
            on_line=on_line,
        )
    subprocessbuild.refuse_patched_context(context_dir, environment)
    if not developing:
        subprocessbuild.check_environment(
            environment,
            pin=pin,
            generator=format_generator_chain(
                read_generator_chain(context_dir / BUILD_CONTEXT_FILE)
            ),
            zephyr_constraint=model.toolchain.zephyr_constraint,
        )
    if on_step is not None:
        on_step("environment", build_environment=environment.described(), fetched=False)
    lock_context(context_dir)
    if on_step is not None:
        on_step("compile", container_image="", **_reported(limits))
    root = containerbuild.resolve_cache_root(
        options=replace(options, cache_root=cache_root or options.cache_root), env=dict(env)
    )
    return subprocessbuild.run_locked_build(
        context_dir,
        environment=environment,
        sdk_sources=sources,
        work_root=work_root / "backend",
        env=dict(env),
        limits=limits,
        sdk_max_bytes=options.sdk_max_bytes,
        # The cache root is resolved the way a container build resolves
        # it, so that a machine nobody configured still has a compiler
        # cache and has only one: unset means the user's cache directory,
        # and a caller without a home directory gets a slow build rather
        # than a refusal. The tiers on top of it are this profile's, and
        # each of them may be moved somewhere else outright.
        cache_root=root,
        tiers=resolve_cache_tiers(
            cache_root=root,
            local=options.cache_local,
            shared=options.cache_shared,
            session=options.cache_session,
            project=options.cache_project,
        ),
        registry=packages,
        on_line=on_line,
        should_stop=should_stop,
    )


def _developer_environment(
    execution: SubprocessExecution,
) -> subprocessbuild.Environment | None:
    """The developer's own workspace, or ``None`` for the store.

    One setting decides it, and what it names is a *whole* environment
    rather than half of one: a west workspace carries the sources, its
    manifest repository is the SDK, and the tools are the ones on the
    ``PATH`` the build was started from. There is nothing to state
    alongside it and nothing to keep in step with it.
    """
    workspace = execution.dev_workspace
    if workspace is None:
        return None
    return subprocessbuild.environment_from_workspace(workspace)


async def _run_subprocess(request: BuildRequest, execution: SubprocessExecution) -> BuildResult:
    """Local, without a container: :func:`compose_local_build`, offloaded.

    The mirror of :func:`_run_local`, through the same module-global
    seam and with the same reason for being offloaded: underneath it
    drives a child process and blocks until it ends.
    """
    options = options_for(request)
    stop = _StopSwitch(request.should_stop)
    result = await asyncio.to_thread(
        compose_local_build,
        request.model,
        signing_pub=request.signing_pub,
        sdk_sources=options.sdk_sources,
        work_root=_work_root(request, ".mcuhome-local"),
        env=dict(request.env),
        project_root=request.project_root,
        registries=request.registries,
        cache_root=execution.cache_root,
        context_dir=request.context_dir,
        on_line=request.on_line,
        on_step=request.on_step,
        mode=MODE_SUBPROCESS,
        environment=_developer_environment(execution),
        options=options,
        stated_container_image=execution.stated_container_image,
        should_stop=stop.armed,
    )
    outcome = result.outcome
    _refuse_unsupported(outcome.status, ran=MODE_SUBPROCESS)
    return BuildResult(
        ok=outcome.ok,
        target=TARGET_LOCAL,
        device=request.model.device.name,
        context_id=outcome.context_id,
        artifacts=tuple(outcome.artifacts),
        out_dir=result.out_dir,
        report=BUILD_REPORT_FILE,
        # No image ran, and an empty reference is the honest answer: the
        # environment is named by its packages, which travel on the
        # composition's own result.
        container_image="",
        # Stopped means the build did not get there because it was
        # stopped. A predicate that turned true while the last step was
        # already succeeding stopped nothing.
        stopped=stop.stopped and not outcome.ok,
        detail=result,
    )


async def _run_local(request: BuildRequest, execution: ContainerExecution) -> BuildResult:
    """Local, in a container: :func:`compose_local_build`, offloaded.

    Synchronous underneath — it drives ``docker`` with a subprocess per
    invocation — so it is offloaded rather than awaited. The composition
    is looked up as a module global so that a caller (or a test) that
    replaced ``compose_local_build`` is the one that runs.
    """
    options = options_for(request)
    stop = _StopSwitch(request.should_stop)
    result = await asyncio.to_thread(
        compose_local_build,
        request.model,
        signing_pub=request.signing_pub,
        sdk_sources=options.sdk_sources,
        work_root=_work_root(request, ".mcuhome-local"),
        env=dict(request.env),
        project_root=request.project_root,
        registries=request.registries,
        container_image=execution.container_image,
        cache_root=execution.cache_root,
        context_dir=request.context_dir,
        on_line=request.on_line,
        on_step=request.on_step,
        options=options,
        should_stop=stop.armed,
    )
    outcome = result.outcome
    _refuse_unsupported(outcome.status, ran=MODE_CONTAINER)
    return BuildResult(
        ok=outcome.ok,
        target=TARGET_LOCAL,
        device=request.model.device.name,
        context_id=outcome.context_id,
        artifacts=tuple(outcome.artifacts),
        out_dir=result.out_dir,
        report=BUILD_REPORT_FILE,
        container_image=result.container_image,
        # See the subprocess execution above: a build that produced its
        # artifacts was not stopped, whenever the predicate turned.
        stopped=stop.stopped and not outcome.ok,
        detail=result,
    )


def _refuse_developer_context(context_dir: Path) -> None:
    """A context of a development build is never sent to a build server.

    Such a context names no build environment — there is none to name:
    its sources are somebody's checkout and its tools are whatever was on
    their ``PATH``. A server finds an environment by the packages a
    context pins, so it could only refuse this one, and it would refuse
    it after the upload. Saying so here costs a directory read and saves
    a gigabyte.

    Read off the directory rather than taken from the caller, because a
    caller that hands over a context is exactly the caller that did not
    create it.
    """
    request_path = Path(context_dir) / CONTEXT_FILE
    if not request_path.is_file():
        return
    if not isinstance(read_context_request(request_path).build_environment, DeveloperEnvironment):
        return
    raise RemoteNotConfigured(
        "This build context was created for a development build and cannot be sent to "
        "a build server.",
        hint=(
            "it names no build environment, because it compiles a workspace you "
            "maintain on this machine — build it here (mcuhome build), or create a "
            "context against MCUHome's own build environment and send that"
        ),
    )


def _remote_context(request: BuildRequest, work_root: Path) -> Path:
    """The base context a remote build sends, at ``<work root>/context``.

    Placed exactly where a local build places its own and written by the
    same function, so the two targets differ in where the context goes
    and in nothing about what it is.

    **Every pin is resolved here, and by the same code** — which is the
    point of pinning on the client. It needs a package index and nothing
    else: no container runtime, no image on this machine, and no round
    trip to the build server. A laptop with no docker at all can
    therefore state which SDK and which build-environment packages its
    firmware must be compiled with, and the server's part shrinks to
    finding an environment that delivers them.
    """
    context_dir = Path(work_root) / "context"
    if request.on_step is not None:
        request.on_step("environment")
    options = options_for(request)
    create_build_context(
        request.model,
        out_dir=context_dir,
        work_root=Path(work_root),
        sdk_sources=options.sdk_sources,
        workspace_sources=options.workspace_sources,
        tools_sources=options.tools_sources,
        sdk_max_bytes=options.sdk_max_bytes,
        signing_pub=request.signing_pub,
        registry=_package_registry(
            request.model,
            project_root=request.project_root,
            registries=request.registries,
            work_root=Path(work_root),
            on_line=request.on_line,
        ),
        hosts=_package_hosts(
            project_root=request.project_root,
            registries=request.registries,
            work_root=Path(work_root),
            on_line=request.on_line,
        ),
        on_line=request.on_line,
    )
    if request.on_step is not None:
        request.on_step(
            "environment",
            build_environment=read_context_facts(context_dir)["build_environment"],
            zephyr="",
            found_under="",
            fetched=False,
        )
    return context_dir


async def _run_remote(request: BuildRequest, target: RemoteBuild) -> BuildResult:
    """The ``remote`` target: :func:`…sessionclient.run_remote_build`.

    The session client is imported here rather than at module level so
    that importing this package costs neither its weight nor the
    ``remote`` extra: ``aiohttp`` and ``zstandard`` are refused in words
    the first time a frame would be sent, not at import.

    One thing is refused before that, because it cannot be invented:
    **the server address** belongs to the caller — a build server has no
    default, and a wrong one is a build context sent to a stranger.

    **The SDK pin is not refused here**, because it is not this target's
    to invent either: it resolves exactly as a local build's does. The
    context this target creates carries the SDK package's version *and*
    its sha256 — the version is the key the server resolves the package
    by, the hash is what it verifies the bytes it found against, which is
    what makes "same version, other bytes" a typed refusal there instead
    of a silent build against another SDK. Both come from the client's
    own source directories where any are configured and from the
    registry index otherwise, the same two tiers in the same order as
    every other target, and only when neither can answer is there a
    refusal — from the pin resolution itself, naming what would supply
    one. What no remote build does is fall back to whatever the server
    happened to have: a context pinned to that would be an identity that
    describes nothing.

    With the address in hand this is a local build's own composition with
    a socket in place of a container: resolve the pin, write the base
    context through the seam both targets share
    (:func:`~mcuhome.workbench.contextdir.create_build_context`), and
    hand the directory to the session client. It is the **base** context
    that goes — unlocked, without a ``manifest.yaml`` — because freezing
    it is the server's act, and the client's duty is to compare the
    identity it answers with. An embedder that already
    holds a context passes it as :attr:`BuildRequest.context_dir` and
    none of this runs.
    """
    if not target.server:
        raise RemoteNotConfigured(
            "A remote build needs the address of a build server, and none is set.",
            hint=(
                "configure a builder once, or name the server outright:\n"
                "    builder:                     # mcuhome.yaml, or your user/system\n"
                "      attic:                     # configuration.yaml\n"
                "        target: remote\n"
                "        server: <host[:port]>\n"
                "    with its token in secrets/builder/attic.yaml, selected via\n"
                "    --builder attic or once via build.builder;\n"
                "or fully manually:\n"
                "    --build-target remote --build-server <host[:port]> "
                "[--build-server-token <token>]\n"
                "A build server is not discovered and has no default: the build "
                "context carries the device model, so the address is a decision "
                "rather than a lookup."
            ),
        )
    url = websocket_url(target.server)
    work_root = _work_root(request, ".mcuhome-remote")
    context_dir = Path(request.context_dir) if request.context_dir is not None else None
    if context_dir is not None:
        _refuse_developer_context(context_dir)
    if context_dir is None:
        if request.on_step is not None:
            request.on_step("context")
        # Off the event loop: this hashes nothing large, but it reads an
        # index, writes the model and the key, and copies the patch set —
        # filesystem work with no await in it, in a target whose whole
        # point is that a caller's loop keeps running while it waits.
        context_dir = await asyncio.to_thread(_remote_context, request, work_root)
        if request.on_step is not None:
            # No `id` among these: freezing the context is the server's
            # act, so a base context on its way out has none yet.
            request.on_step("context", **read_context_facts(context_dir))

    from mcuhome.workbench import sessionclient

    if request.on_step is not None:
        request.on_step("compile", server=target.server)
    stop = _StopSwitch(request.should_stop)
    result = await sessionclient.run_remote_build(
        context_dir,
        url=url,
        token=target.token,
        work_root=work_root,
        image=target.container_image,
        on_line=request.on_line,
        on_wait=request.on_wait,
        wait=target.wait,
        max_wait=target.max_wait_seconds,
        should_stop=stop.armed,
    )
    _refuse_unsupported(result.status, ran=TARGET_REMOTE)
    # Either side may have ended it: this one asked, or the server
    # cancelled the invocation for a reason of its own — an operator, a
    # session that was taken away. Both are a build that was stopped
    # rather than one that failed, and the verdict is the server's own
    # word for it.
    ended = stop.stopped or result.status == sessionclient.STATUS_CANCELLED
    return BuildResult(
        ok=result.ok,
        target=TARGET_REMOTE,
        device=request.model.device.name,
        context_id=result.context_id,
        artifacts=tuple(result.artifacts),
        out_dir=result.out_dir,
        report=BUILD_REPORT_FILE,
        # What actually built it, in the same form a local container
        # build records: the server chose the delivery and is the only
        # side that can say which one, so a record without this would
        # name the packages and not the bytes.
        container_image=result.container_image,
        # A verdict of success is neither, however late the stop came.
        stopped=ended and not result.ok,
        detail=result,
    )
