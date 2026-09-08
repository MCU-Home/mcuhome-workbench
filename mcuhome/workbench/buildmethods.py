# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build methods behind one interface.

``local`` drives a build environment on this machine through the build
environment specification, ``remote`` drives a build server through the
session protocol. They differ in almost everything — one starts a container, one
opens a WebSocket — and in exactly the thing a caller cares about they do
not differ at all: both deliver an **unsigned** image plus a build report,
and the signature is a separate host-side step afterwards (E55, E56). That
is what makes one interface possible rather than merely tidy.

So this module is small on purpose. :func:`build_firmware` takes a
resolved device model plus the inputs a build needs, runs the one the
target names, and answers with one :class:`BuildOutcome` whose meaning
does not depend on which ran: *did it succeed*, and *where are the
unsigned artifacts and the report* — enough for the shared signing step,
and enough for a caller that only wants to know whether to carry on. What
is genuinely composition-specific — a container reference, an invocation
id — travels in :attr:`BuildOutcome.detail`, typed as itself, so a
renderer can reach it without every consumer having to.

**Two axes, and a name is one word for both.** Where a build runs and how
the machine that runs it executes it are separate decisions and only the
first belongs to a caller, which is what :mod:`…buildtarget` states:
:class:`~mcuhome.workbench.buildtarget.LocalBuild` carries an
:class:`~mcuhome.workbench.buildtarget.Execution`,
:class:`~mcuhome.workbench.buildtarget.RemoteBuild` deliberately carries
none. :func:`build_firmware` is the seam that takes one of those, and
:func:`run_build` is the name-shaped entry point over it for a caller
whose method arrived as a flag or a configuration value —
:func:`target_for_method` is the whole of the translation, in one place,
so that the method-specific fields of :class:`BuildRequest` have exactly
one reader.

**No key of any kind can be private here.** The one field that carries
key material is :attr:`BuildRequest.signing_pub`, the PEM that becomes
``keys/signing.pub`` in a build context — the public half, and all of the
key pair a build ever sees. There is no slot a private key fits in, on
either method and by construction, which is the structural half of the
invariant that the signing key never leaves the machine ``mcuhome`` runs
on.

**Nothing here reaches outside this package.** The thing that drives a
build container is this package's own
(:mod:`mcuhome.workbench.containerbuild`), and no build method runs a
compiler in this process. "A build needs a container runtime and nothing
else of a toolchain" was the claim from the start, and it is true at the
level of installed distributions: ``mcuhome-compiler`` is what a *build
environment* carries, and this package never imports it. The one host-side
call into it left is code generation for its own sake
(:mod:`mcuhome.workbench.generate`), which no build takes.

**Awaitable, because builds wait** (E16, E21). ``remote`` is asynchronous
throughout and ``local`` blocks for minutes in a subprocess, so the one
interface over them is ``async`` and the synchronous method is offloaded
to a thread. A command line wraps the whole thing in one
:func:`asyncio.run` at its entry point and its user sees nothing.

**Both methods are complete** (E65). ``remote`` was the last one
that could not start from a device model: a build context is
content-addressed over the SDK package's hash, and nothing resolved that
pin on this path. It does now, through the *same* resolver and the same
context writer the ``local`` method uses
(:func:`~mcuhome.workbench.resolve_pins.resolve_sdk`,
:func:`~mcuhome.workbench.contextdir.create_build_context`), from the
same ``--sdk-sources`` directories. There is deliberately **no**
capabilities round trip for it, exactly as there is none for the
container since E61: the client states a version *and* a sha256, the
server resolves the version against its own sources — which may be a
cache, a package service or a private registry — and verifies the bytes
it found against the hash. Same number, other bytes is a typed refusal
on that side, never a quiet build against another SDK; and because the
hash is what the identity is computed over, none of this changes the
context format or the ID rule.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcuhome.model.artifacts import Artifact
from mcuhome.model.buildimage import DEFAULT_ENVIRONMENT
from mcuhome.model.context import (
    BUILD_CONTEXT_FILE,
    CONTEXT_FILE,
    DeveloperEnvironment,
    format_generator_chain,
)
from mcuhome.model.errors import BuildError, ConfigError
from mcuhome.model.imageref import parse_reference
from mcuhome.model.model import DeviceModel

from mcuhome.workbench import buildenvstore, containerbuild, subprocessbuild
from mcuhome.workbench.buildenvsession import EnvironmentUnavailable, cache_tiers
from mcuhome.workbench.buildlock import build_lock
from mcuhome.workbench.buildtarget import (
    BUILD_MODES,
    DEFAULT_BUILD_MODE,
    DEFAULT_CONTAINER_REPOSITORIES,
    DEFAULT_MAX_WAIT_SECONDS,
    MODE_CONTAINER,
    MODE_SUBPROCESS,
    BuildTarget,
    ContainerExecution,
    Execution,
    LocalBuild,
    RemoteBuild,
    SubprocessExecution,
)
from mcuhome.workbench.configuration import Settings, resolve_settings
from mcuhome.workbench.contextdir import (
    context_facts,
    create_build_context,
    lock_context,
    read_context_request,
    read_generator_chain,
)
from mcuhome.workbench.imgtool import BUILD_REPORT_FILE
from mcuhome.workbench.project import Project
from mcuhome.workbench.resolve_pins import package_reference

if TYPE_CHECKING:  # pragma: no cover - types only
    # Imported for the annotations alone. The registry client is reached
    # at call time (see `_package_registry`), and importing it here for a
    # type would put it in the import path of every build method.
    from mcuhome.workbench.packageregistry import RegistrySettings, RegistrySource

__all__ = [
    "BUILD_MODES",
    "DEFAULT_BUILD_MODE",
    "DEFAULT_MAX_WAIT_SECONDS",
    "LOCAL",
    "METHODS",
    "MODE_CONTAINER",
    "MODE_SUBPROCESS",
    "REMOTE",
    "BuildOptions",
    "BuildOutcome",
    "BuildRequest",
    "BuildTarget",
    "ContainerExecution",
    "Execution",
    "LocalBuild",
    "RemoteBuild",
    "RemoteNotConfigured",
    "SubprocessExecution",
    "UnknownBuildMode",
    "UnknownMethod",
    "build_firmware",
    "build_options",
    "compose_container_build",
    "compose_subprocess_build",
    "options_for",
    "refuse_retired_environment_field",
    "websocket_url",
    "resolve_build_mode",
    "resolve_method",
    "run_build",
    "target_for_method",
]

#: Drives a build environment on this machine, through the build
#: environment specification. The default: it is the method that needs a
#: container runtime and nothing else of a toolchain, and the one whose
#: private key never reaches the thing that compiles.
LOCAL = "local"

#: The same as ``local``, through a build server (ADR 0019).
REMOTE = "remote"

#: Every method name, in the order a refusal lists them.
METHODS = (LOCAL, REMOTE)

#: What a caller that expressed no preference gets (E54).
DEFAULT_METHOD = LOCAL

#: How the ``local`` method executes a build on this machine: the two
#: values of the ``build.mode`` configuration key, and the two
#: executions :mod:`mcuhome.workbench.buildtarget` distinguishes — in a
#: build container, or as a child process against a build environment
#: unpacked on the host. Defined with those classes and re-exported
#: here, so that reading a configuration file does not pull in the
#: module that dispatches builds. :data:`DEFAULT_BUILD_MODE` is what a
#: caller that expressed no preference gets: the container, because it
#: is the mode that needs a container runtime and nothing else of a
#: toolchain, and the only one that isolates a build context — which is
#: untrusted input, because it carries patches.

LineSink = Callable[[str], None]


class UnknownMethod(BuildError):
    """A build method by a name that is not one of :data:`METHODS`."""


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

    Two shapes, and they are the two decisions this method cannot make
    for a caller: **where** to build — there is no default build server
    and no discovery — and **which SDK package** the context pins, which
    is resolved from the caller's own sources and is part of the
    identity the work is attributed to. Both refusals name the knob that
    supplies the value rather than guessing at one, because a guess here
    is either a context sent to a stranger or an identity that describes
    a build nobody asked for.
    """


def resolve_method(name: str | None) -> str:
    """The build method *name* selects, or a refusal listing the real ones.

    ``None`` and the empty string mean "no preference" and resolve to
    :data:`DEFAULT_METHOD`, so a caller can hand through whatever its own
    configuration ladder produced without checking it first.
    """
    if not name:
        return DEFAULT_METHOD
    if name in METHODS:
        return name
    raise UnknownMethod(
        f'"{name}" is not a build method MCUHome knows.',
        hint=(
            "the build methods are "
            + ", ".join(METHODS)
            + f": {LOCAL} compiles in a build environment on this "
            f"machine, and {REMOTE} on a build server"
        ),
    )


def resolve_build_mode(name: str | None) -> str:
    """The build mode *name* selects, or a refusal listing the real ones.

    ``None`` and the empty string mean "no preference" and resolve to
    :data:`DEFAULT_BUILD_MODE`, so a caller can hand through whatever its
    own configuration ladder produced without checking it first — the
    same contract :func:`resolve_method` has for the axis beside this one.
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

    Everything in here is a property of **this machine**: which of the
    two executions it uses, where it keeps unpacked build environments,
    which interpreter creates their virtual environments, how much a
    package may unpack to, where its compiler cache tiers are. None of it
    describes the firmware, which is why none of it lives in a device and
    all of it is configuration.

    It travels as one object rather than as a dozen fields on
    :class:`BuildRequest` for two reasons. A caller that has resolved the
    configuration hands over what it resolved
    (:func:`build_options`), and a caller that has not — an embedder
    driving a bare model — gets the machine's own answer without having
    to know that these keys exist (:func:`options_for`). And the build
    compositions take one parameter instead of growing one per key.

    Unset values are ``None`` throughout and mean *the default of
    whatever consumes them*, never a value invented here: the store
    resolves its own location from the user's cache home, the cache tiers
    fall back to the cache root, and the sources fall back to
    ``build.sdk_sources`` — so an option nobody set changes nothing.
    """

    #: ``build.mode``: ``container`` or ``subprocess``.
    mode: str = DEFAULT_BUILD_MODE
    #: Where ``mode`` came from, in the words :class:`…configuration.Setting`
    #: uses — the file, the variable, or ``default``. Carried so that a
    #: refusal caused by the mode can say who chose it.
    mode_source: str = "default"
    #: ``build.container_repositories``: where a container build may take
    #: its environment from, in search order. The image is chosen by the
    #: package set its labels declare, so this list says whose images may
    #: deliver one and never which image is used.
    container_repositories: tuple[str, ...] = DEFAULT_CONTAINER_REPOSITORIES
    #: ``build.env_store``: the store's root. ``None`` is the user's
    #: cache home, which is where a machine nobody configured keeps it.
    env_store: Path | None = None
    #: ``build.dev_workspace``: a west workspace the developer maintains,
    #: built against instead of the environment MCUHome provisions.
    dev_workspace: Path | None = None
    #: ``build.python``: the interpreter that creates a build
    #: environment's virtual environment. ``None`` is the one MCUHome
    #: itself runs on, which is right whenever the host's Python is the
    #: one the tools package was built for.
    python: str | None = None
    #: ``build.workspace_sources`` / ``build.tools_sources``: operator
    #: directories per package kind. Empty falls back to
    #: ``build.sdk_sources``, so one directory holding everything keeps
    #: working.
    workspace_sources: tuple[Path, ...] = ()
    tools_sources: tuple[Path, ...] = ()
    #: ``build.<kind>_max_bytes``: how much each package may unpack to.
    sdk_max_bytes: int | None = None
    workspace_max_bytes: int | None = None
    tools_max_bytes: int | None = None
    #: ``build.cache_*``: the compiler cache tiers. ``cache_local`` and
    #: ``cache_shared`` name a tier outright; unset, both are laid out
    #: under the cache root the build already resolves.
    cache_local: Path | None = None
    cache_shared: Path | None = None
    cache_session: Path | None = None
    cache_project: Path | None = None

    def bound(self, kind: str) -> int | None:
        """The configured unpacking bound for a package *kind*, if any."""
        return {
            buildenvstore.SDK_KIND: self.sdk_max_bytes,
            buildenvstore.WORKSPACE_KIND: self.workspace_max_bytes,
            buildenvstore.TOOLS_KIND: self.tools_max_bytes,
        }.get(kind)


def build_options(settings: Settings) -> BuildOptions:
    """The ``build`` section of a resolved configuration, as one object.

    Every value comes out of the registry that declared it — this
    function knows the names and nothing else, so a key's kind, default,
    validation and the five layers it merged through are stated in
    exactly one place (:data:`mcuhome.workbench.configuration.OPTIONS`).

    One key of the section is deliberately not here:
    ``build.sdk_sources`` is a field of the request itself
    (:attr:`BuildRequest.sdk_sources`), because the remote method needs
    it too and never reads these options at all. A caller that resolves
    the configuration states both.
    """
    setting = settings.setting("build.mode")

    def path(name: str) -> Path | None:
        value = settings.value(name)
        return None if value is None else Path(value)

    def number(name: str) -> int | None:
        # A bound the registry answered with its own default is not a
        # statement: the store's table is the same value, and passing it
        # on would make every kind look configured.
        return int(settings.value(name)) if settings.origin(name) != "default" else None

    return BuildOptions(
        mode=resolve_build_mode(setting.value),
        mode_source=setting.source or setting.origin,
        container_repositories=tuple(settings.value("build.container_repositories")),
        env_store=path("build.env_store"),
        dev_workspace=path("build.dev_workspace"),
        python=settings.value("build.python") or None,
        workspace_sources=tuple(settings.value("build.workspace_sources")),
        tools_sources=tuple(settings.value("build.tools_sources")),
        sdk_max_bytes=number("build.sdk_max_bytes"),
        workspace_max_bytes=number("build.workspace_max_bytes"),
        tools_max_bytes=number("build.tools_max_bytes"),
        cache_local=path("build.cache_local"),
        cache_shared=path("build.cache_shared"),
        cache_session=path("build.cache_session"),
        cache_project=path("build.cache_project"),
    )


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
    return build_options(resolve_settings(project=project, env=request.env))


@dataclass(frozen=True)
class BuildRequest:
    """Everything a build method may be given, whichever one runs.

    :attr:`model` and :attr:`out_dir` are the two every method needs: what
    to build, and the durable directory the unsigned artifacts and the
    build report end up beside. Everything below them is optional and
    named for the method it serves; a field a method does not use is
    ignored rather than refused, because a caller assembling one request
    for a method chosen at run time should not have to assemble three.

    The fields marked for one method are the ones a build target states
    instead (:mod:`mcuhome.workbench.buildtarget`), and while both exist
    they are read in exactly one place — :func:`target_for_method`, which
    is what :func:`run_build` turns a method name into. A caller that
    builds a target itself and calls :func:`build_firmware` puts those
    values on the target, and what it leaves here is ignored.
    """

    #: The canonical device model, stages 1-3 already run.
    model: DeviceModel
    #: The build directory: where a user looks afterwards, and where the
    #: shared signing step reads the report from.
    out_dir: Path
    #: The environment to resolve tools, images and caches from — stated,
    #: never read from the process (:mod:`mcuhome.model.userpaths`).
    env: Mapping[str, str] = field(default_factory=dict)
    #: Parallel compile jobs.
    jobs: int = 1
    #: The pristine mode the ``remote`` method's session protocol still
    #: carries. A local build ignores it: under build-environment
    #: specification generation 3 every step starts pristine by
    #: definition (§3), so there is nothing to ask for.
    mode: str = "clean"
    #: Where the build log goes, line by line, while it happens.
    on_line: LineSink | None = None
    #: Called with a step key when the build enters a new step —
    #: ``"context"`` when the build context is being created,
    #: ``"compile"`` when the build environment starts compiling. The
    #: honest-progress seam of cli ADR 0004: a caller renders steps it
    #: was told about, never ones it guessed. Keys are append-only
    #: vocabulary; consumers ignore keys they do not know.
    #:
    #: A step may be reported a second time with keyword **facts** once
    #: it knows something worth stating — which SDK the context pinned,
    #: which image answered the Zephyr requirement. Facts are display
    #: material and append-only in the same way: a consumer renders what
    #: it recognizes and ignores the rest, and a build method that has
    #: nothing to say states nothing rather than inventing it.
    on_step: Callable[..., None] | None = None
    #: Scratch area a method may own. Defaults to a hidden directory
    #: under :attr:`out_dir`, which is what a command line wants: rebuilt
    #: every run, thrown away with the build directory.
    work_root: Path | None = None

    # -- local / remote -----------------------------------------------
    #: PEM of the user's MCUboot **public** key. Becomes
    #: ``keys/signing.pub`` in the build context, which is all of the key
    #: pair a build ever sees (ADR 0015 decision 8).
    signing_pub: str = ""

    # -- local / remote ------------------------------------------------
    #: Directories holding the hash-pinned MCUHome SDK package (ADR 0018).
    #: Read by **both** container-shaped methods, for the same reason and
    #: at the same moment: the resolved package hash is an input of the
    #: context ID, so the pin has to exist before a context does (E65).
    #: What differs afterwards is who fetches the bytes — this machine for
    #: ``local``, the build server out of its operator's own sources for
    #: ``remote``, which then verifies them against this pin.
    sdk_sources: Sequence[Path] = ()
    #: The project directory, which is where the trust anchors are:
    #: ``secrets/trust-anchor/<base-domain>.json``, written when the
    #: project was created. Left ``None`` the build has no project to
    #: read them from and therefore no registry — it then builds from
    #: :attr:`sdk_sources` alone, which is exactly what an embedder
    #: driving a bare model wants.
    project_root: Path | None = None
    #: What the project says about package registries, resolved from
    #: configuration (the ``registry`` option): mirror overrides per
    #: source, and which registries are marked untrusted. Empty means the
    #: defaults — the official registry, its own mirror list, verified.
    registries: Sequence[RegistrySettings] = ()

    # -- local ---------------------------------------------------------
    #: What the ``build`` section of this machine's configuration says
    #: (:class:`BuildOptions`). ``None`` — the ordinary case — resolves
    #: it here, from :attr:`env` and :attr:`project_root`; a caller that
    #: has already resolved the configuration states the result and is
    #: answered with exactly that.
    options: BuildOptions | None = None
    #: How this machine executes the build: ``container`` (what every
    #: build did before this existed) or ``subprocess``. ``None`` takes
    #: the ``build.mode`` configuration key, which is where the answer
    #: ordinarily comes from; a caller that builds a
    #: :class:`~mcuhome.workbench.buildtarget.BuildTarget` itself states
    #: the execution instead, and one that states a mode here overrides
    #: the configuration for this build.
    build_mode: str | None = None
    #: Build-container reference to compile in; ``None`` takes the default.
    image: str | None = None
    #: Where the compiler cache lives on this machine. ``None`` takes the
    #: user's cache directory, which is what every build does unless
    #: somebody moved it — one cache per user, shared by every project.
    ccache_dir: Path | None = None
    #: A development build, for ``build.mode = subprocess`` only: a west
    #: workspace the developer maintains, built against instead of the
    #: environment MCUHome provisions into its store. ``None`` — the
    #: ordinary case — takes the ``build.dev_workspace`` configuration
    #: key, which is unset on a machine that is not developing the SDK
    #: itself.
    dev_workspace: Path | None = None

    # -- remote --------------------------------------------------------
    #: The build server's address, as a person writes it: a host, a
    #: ``host:port``, or either with a scheme. :func:`websocket_url` turns
    #: it into the URL the socket needs. *Selecting* a server belongs to
    #: the caller — a configured builder or the fully manual
    #: ``--build-mode`` rung (ADR 0023);
    #: :func:`mcuhome.workbench.configuration.resolve_builder` is the
    #: configured path — and an address is what arrives here.
    server: str | None = None
    #: The bearer token for it, from the builder's
    #: ``secrets/build-server/<name>.yaml`` (or the manual rung's
    #: ``--build-token``). ``None`` sends no ``Authorization`` header at
    #: all, which this package permits because a third-party server may
    #: want none.
    token: str | None = None
    #: A build context directory to build instead of creating one. For a
    #: caller that already holds one — an embedder that assembled a
    #: context elsewhere, a build server that received one over a socket,
    #: a test driving a hand-written one. Left ``None``, which is the
    #: ordinary case, the build creates its own from :attr:`model` and
    #: :attr:`sdk_sources`. Either way it is a *base* context: locking it
    #: is the act of whoever builds it, and a client that sent one checks
    #: the identity the server answers with (E37).
    context_dir: Path | None = None
    #: Wait when the build server has no room. A busy server hands out a
    #: turn instead of a session, and waiting for it is what a person
    #: starting a build almost always wants; ``False`` is the caller that
    #: would rather be told now.
    wait_for_turn: bool = True
    #: How long that wait may last in total, in seconds. ``0`` removes
    #: the bound.
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS
    #: Called with a
    #: :class:`~mcuhome.workbench.sessionclient.SeatWait` each time a
    #: turn is refused, so a caller can say something true while nothing
    #: is happening. Deliberately **not** a step: the build has not
    #: started and may never start, and a step bar claiming otherwise
    #: would be showing progress that does not exist.
    on_wait: Callable[[Any], None] | None = None


@dataclass(frozen=True)
class BuildOutcome:
    """What a build method produced, in the one shape every method answers.

    :attr:`successful` is the only field a caller must consult before the
    others mean anything. :attr:`out_dir` is where the **unsigned**
    artifacts and the build report are, and :attr:`report` is that
    report's file name — the two together are what the one shared signing
    step needs, and they are the whole reason this class exists (E56).

    :attr:`artifacts` is the declared artifact set, read out of the legacy
    container invocation's result document (retired at the switchover) in
    :class:`~mcuhome.model.artifacts.Artifact` — the same type whichever
    method produced it.
    """

    #: Which of :data:`METHODS` ran.
    method: str
    successful: bool
    #: The method's own word for the result: ``success``/``failure`` from
    #: a legacy container invocation result document (retired at the
    #: switchover).
    status: str
    #: The identity the work is attributed to: the build context's ID.
    context_id: str
    artifacts: tuple[Artifact, ...]
    #: Where the unsigned artifacts and the report are.
    out_dir: Path | None
    #: The build report's file name in :attr:`out_dir`: the legacy
    #: container invocation's (retired at the switchover)
    #: ``build-report.json``, which carries the imgtool parameters the
    #: host signer needs (E55).
    report: str
    #: The build-container reference, where one was used.
    image: str = ""
    #: The method's own result object, untouched.
    detail: Any = None


def _work_root(request: BuildRequest, name: str) -> Path:
    return Path(request.work_root) if request.work_root else Path(request.out_dir) / name


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
    socket; :func:`~mcuhome.workbench.packageregistry.registry_factory`
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
    from mcuhome.workbench.packageregistry import OFFICIAL_BASE_DOMAIN, registry_factory

    reference = parse_reference(model.sources.sdk, default_registry=OFFICIAL_BASE_DOMAIN)
    return registry_factory(
        reference.registry,
        project_root=Path(project_root),
        settings=tuple(registries),
        into=Path(work_root) / "registry",
        on_warning=on_line,
    )


def _refuse_image_without_container(image: str, *, source: str) -> ConfigError:
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
        f"This build was given the container image {image}, and it is set to "
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
            f"    mcuhome device build <device> --build-method local\n"
            f"or unset {subprocessbuild.DEV_WORKSPACE_OPTION} to build on the server."
        ),
    )


def refuse_retired_environment_field(model: DeviceModel) -> None:
    """A device that still names a build container is refused, not ignored.

    ``sources.build_environment`` named the image a build ran in, back
    when an image was chosen by the Zephyr release it declared. A build
    environment is now identified by its **packages**, and the image that
    delivers them is found by its labels — so the field names a thing
    that is no longer selected that way, and its old default names an
    image that no longer exists.

    Ignoring it would be the worse of the two answers: the device says
    which environment to build in, the build would use another one, and
    nothing about the result would say so. The persistent pin returns
    under its own name once the device schema carries it.
    """
    stated = model.sources.build_environment
    if not stated or stated == DEFAULT_ENVIRONMENT:
        return
    raise ConfigError(
        f'This device names sources.build_environment: "{stated}", and that setting is retired.',
        hint=(
            "a build environment is named by its packages now — sources.build_workspace "
            "and sources.build_tools — and the container image that delivers them is "
            "found by the packages it declares. Remove the entry from the device; to "
            "pin one image for a single build, pass it to that build instead."
        ),
    )


def target_for_method(method: str | None, request: BuildRequest) -> BuildTarget:
    """The build target a method *name* and a request describe together.

    The bridge between the two vocabularies while both exist: a name is
    one word for two decisions, and a target states them apart
    (:mod:`mcuhome.workbench.buildtarget`). Everything method-specific on
    :class:`BuildRequest` is read **here and nowhere else**, so that the
    day those fields move onto the targets there is one call site to
    change rather than three build methods.

    *method* goes through :func:`resolve_method` first, so ``None`` and
    the empty string mean the default and an unknown name is the same
    refusal a caller would have got from ``run_build``.

    **This is also where the configuration is consulted** for the two
    values a request may leave open — the mode and the development
    workspace (:func:`options_for`) — because they answer the same
    question the method-specific fields answer and must be read in one
    place with them. What the request states wins over what the machine
    is configured to do: a caller that named a value meant it.
    """
    chosen = resolve_method(method)
    if chosen == LOCAL:
        options = options_for(request)
        mode = resolve_build_mode(request.build_mode) if request.build_mode else options.mode
        if mode == MODE_SUBPROCESS:
            if request.image is not None:
                raise _refuse_image_without_container(
                    request.image,
                    # Whoever chose the mode is who has to be told, and a
                    # mode this request states itself did not come from
                    # any configuration file.
                    source="this build" if request.build_mode else options.mode_source,
                )
            return LocalBuild(
                execution=SubprocessExecution(
                    ccache_dir=request.ccache_dir,
                    dev_workspace=(
                        request.dev_workspace
                        if request.dev_workspace is not None
                        else options.dev_workspace
                    ),
                )
            )
        developing = (
            request.dev_workspace if request.dev_workspace is not None else options.dev_workspace
        )
        if developing is not None:
            raise _refuse_developer_without_subprocess(
                developing,
                source="this build" if request.build_mode else options.mode_source,
            )
        return LocalBuild(
            execution=ContainerExecution(image=request.image, ccache_dir=request.ccache_dir)
        )
    developing = (
        request.dev_workspace
        if request.dev_workspace is not None
        else options_for(request).dev_workspace
    )
    if developing is not None:
        raise _refuse_developer_remotely(developing)
    return RemoteBuild(
        server=request.server,
        token=request.token,
        wait=request.wait_for_turn,
        max_wait_seconds=request.max_wait_seconds,
    )


async def build_firmware(request: BuildRequest, *, target: BuildTarget) -> BuildOutcome:
    """Build *request* at *target*, and answer in the one outcome shape.

    The seam. Above it a caller decides *what* to build (a device model,
    or a build context already created from one) and *where*; below it
    the three compositions differ in everything and agree on the answer.
    It is also where a build server enters: what reaches it over a socket
    is a context and a target of its own making, and from that point on
    the work is the same work a local build does.

    Raises whatever typed refusal the target's composition raises — a
    missing build container, a missing SDK package, a build server that
    said no. A build that ran and *failed* is not an exception: it comes
    back with :attr:`BuildOutcome.successful` false and the composition's
    own account in :attr:`BuildOutcome.detail`, because a failed compile
    is an answer and a caller usually wants to render it rather than
    catch it. A target this package does not implement is a
    :class:`TypeError` rather than a refusal: a name can be mistyped, an
    object cannot.

    The build directory is held for the duration (:mod:`…buildlock`), so
    a second build of it refuses in words instead of deleting this one's
    work under it. Here rather than in a composition, because every one
    of them writes into the same directory and the collision does not
    care which two were running — nor whether the other one is a command
    line or a dashboard.
    """
    with build_lock(request.out_dir, device=request.model.device.name):
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


async def run_build(request: BuildRequest, *, method: str = DEFAULT_METHOD) -> BuildOutcome:
    """Run *method* over *request*: :func:`build_firmware` by method name.

    The name-shaped entry point, kept for callers that select a build
    method from a command-line flag or a configuration value and have
    nothing to say about the two axes apart. It resolves the name to a
    target (:func:`target_for_method`) and hands over; everything the
    seam documents holds here unchanged, including
    :class:`UnknownMethod` for a name that is not one of
    :data:`METHODS`.
    """
    return await build_firmware(request, target=target_for_method(method, request))


def compose_local_build(
    model: DeviceModel,
    *,
    signing_pub: str,
    sdk_sources: Sequence[Path],
    work_root: Path,
    env: dict[str, str],
    project_root: Path | None = None,
    registries: Sequence[RegistrySettings] = (),
    image: str | None = None,
    jobs: int = 1,
    ccache_dir: Path | None = None,
    created: datetime | None = None,
    context_dir: Path | None = None,
    on_line: Any = None,
    on_step: Any = None,
    runtime: Any = None,
    registry: Any = None,
    images: Any = None,
    build_mode: str = DEFAULT_BUILD_MODE,
    environment: Any = None,
    options: BuildOptions | None = None,
):
    """The local build, dispatched to the execution this machine uses.

    One entry point for the two executions, so that a mode a
    configuration produced reaches the right composition without every
    caller learning both: ``container`` is
    :func:`compose_container_build` and ``subprocess` is
    :func:`compose_subprocess_build`. *environment* — the store entries a
    build runs against — belongs to the second alone, and *image*,
    *runtime* and *images* to the first.

    Synchronous, because both compositions are; ``build_firmware``
    offloads them.
    """
    options = options if options is not None else BuildOptions()
    if resolve_build_mode(build_mode) == MODE_SUBPROCESS:
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
            jobs=jobs,
            ccache_dir=ccache_dir,
            context_dir=context_dir,
            on_line=on_line,
            on_step=on_step,
            registry=registry,
            options=options,
        )
    return compose_container_build(
        model,
        signing_pub=signing_pub,
        sdk_sources=sdk_sources,
        work_root=work_root,
        env=env,
        project_root=project_root,
        registries=registries,
        image=image,
        jobs=jobs,
        ccache_dir=ccache_dir,
        created=created,
        context_dir=context_dir,
        on_line=on_line,
        on_step=on_step,
        runtime=runtime,
        registry=registry,
        images=images,
        options=options,
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
    image: str | None = None,
    jobs: int = 1,
    ccache_dir: Path | None = None,
    created: datetime | None = None,
    context_dir: Path | None = None,
    on_line: Any = None,
    on_step: Any = None,
    runtime: Any = None,
    registry: Any = None,
    images: Any = None,
    options: BuildOptions | None = None,
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

    *image* is the one-invocation override, in any of the four pin forms;
    it narrows which images are looked at and never what is accepted.
    *context_dir* is the caller that already holds a **base** context and
    wants this one built — an embedder that assembled one elsewhere, a
    build server that received one over a socket. It is used as it is:
    nothing is written into it but the lock, and no context step is
    announced, because this composition did not create one.

    *runtime*, *registry* and *images* are the three seams a caller may
    replace: the container runtime, the package registry the pins are
    resolved and fetched through (``None`` derives it from the project),
    and the container registry the image labels are read from.
    """
    options = options if options is not None else BuildOptions()
    refuse_retired_environment_field(model)
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
        )
        if on_step is not None:
            # What the context turned out to be, read back off the
            # directory: the step announced itself before any of this was
            # decided, and the decisions are the interesting part.
            on_step("context", **context_facts(context_dir))

    if on_step is not None:
        on_step("environment")
    pin = read_context_request(context_dir / CONTEXT_FILE).build_environment
    resolved = containerbuild.prepare_environment(
        pin,
        env=env,
        repositories=options.container_repositories,
        image_pin=image,
        workspace_source=package_reference(model.sources.build_workspace).source,
        tools_source=package_reference(model.sources.build_tools).source,
        sources=sources,
        workspace_sources=options.workspace_sources,
        tools_sources=options.tools_sources,
        registry=packages,
        images=images,
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
    containerbuild.check_image(
        resolved.declaration,
        reference=resolved.reference,
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
        on_step("compile", image=resolved.reference, jobs=jobs)
    root = containerbuild.cache_root(env, ccache_dir)
    return containerbuild.run_locked_build(
        context_dir,
        image=resolved,
        sdk_sources=sources,
        work_root=work_root / "backend",
        env=dict(env),
        jobs=jobs,
        sdk_max_bytes=options.sdk_max_bytes,
        zephyr_constraint=model.toolchain.zephyr_constraint,
        # The same cache root and the same tiers the subprocess profile
        # is given: one cache per user, laid out once, mounted here and
        # linked there.
        tiers=cache_tiers(
            ccache_dir=root,
            local_dir=options.cache_local,
            shared_ccache_dir=options.cache_shared,
            session_dir=options.cache_session,
            project_dir=options.cache_project,
        ),
        registry=packages,
        runtime=runtime,
        on_line=on_line,
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
    jobs: int = 1,
    ccache_dir: Path | None = None,
    context_dir: Path | None = None,
    on_line: Any = None,
    on_step: Any = None,
    registry: Any = None,
    options: BuildOptions | None = None,
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
    """
    options = options if options is not None else BuildOptions()
    refuse_retired_environment_field(model)
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
            developer=developing,
        )
        if on_step is not None:
            on_step("context", **context_facts(context_dir))

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
            workspace_source=package_reference(model.sources.build_workspace).source,
            tools_source=package_reference(model.sources.build_tools).source,
            sources=sources,
            workspace_sources=options.workspace_sources,
            tools_sources=options.tools_sources,
            store=options.env_store,
            interpreter=options.python,
            bounds={
                kind: bound
                for kind in (buildenvstore.WORKSPACE_KIND, buildenvstore.TOOLS_KIND)
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
        on_step("compile", image="", jobs=jobs)
    cache_root = containerbuild.cache_root(dict(env), ccache_dir)
    return subprocessbuild.run_locked_build(
        context_dir,
        environment=environment,
        sdk_sources=sources,
        work_root=work_root / "backend",
        env=dict(env),
        jobs=jobs,
        sdk_max_bytes=options.sdk_max_bytes,
        # The cache root is resolved the way a container build resolves
        # it, so that a machine nobody configured still has a compiler
        # cache and has only one: unset means the user's cache directory,
        # and a caller without a home directory gets a slow build rather
        # than a refusal. The tiers on top of it are this profile's, and
        # each of them may be moved somewhere else outright.
        ccache_dir=cache_root,
        tiers=cache_tiers(
            ccache_dir=cache_root,
            local_dir=options.cache_local,
            shared_ccache_dir=options.cache_shared,
            session_dir=options.cache_session,
            project_dir=options.cache_project,
        ),
        registry=packages,
        on_line=on_line,
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


async def _run_subprocess(request: BuildRequest, execution: SubprocessExecution) -> BuildOutcome:
    """Local, without a container: :func:`compose_local_build`, offloaded.

    The mirror of :func:`_run_local`, through the same module-global
    seam and with the same reason for being offloaded: underneath it
    drives a child process and blocks until it ends.
    """
    result = await asyncio.to_thread(
        compose_local_build,
        request.model,
        signing_pub=request.signing_pub,
        sdk_sources=tuple(Path(source) for source in request.sdk_sources),
        work_root=_work_root(request, ".mcuhome-local"),
        env=dict(request.env),
        project_root=request.project_root,
        registries=request.registries,
        jobs=request.jobs,
        ccache_dir=execution.ccache_dir,
        context_dir=request.context_dir,
        on_line=request.on_line,
        on_step=request.on_step,
        build_mode=MODE_SUBPROCESS,
        environment=_developer_environment(execution),
        options=options_for(request),
    )
    outcome = result.outcome
    return BuildOutcome(
        method=LOCAL,
        successful=outcome.successful,
        status=outcome.status,
        context_id=outcome.context_id,
        artifacts=tuple(outcome.artifacts),
        out_dir=result.out_dir,
        report=BUILD_REPORT_FILE,
        # No image ran, and an empty reference is the honest answer: the
        # environment is named by its packages, which travel on the
        # composition's own result.
        image="",
        detail=result,
    )


async def _run_local(request: BuildRequest, execution: ContainerExecution) -> BuildOutcome:
    """Local, in a container: :func:`compose_local_build`, offloaded.

    Synchronous underneath — it drives ``docker`` with a subprocess per
    invocation — so it is offloaded rather than awaited. The composition
    is looked up as a module global so that a caller (or a test) that
    replaced ``compose_local_build`` is the one that runs.
    """
    result = await asyncio.to_thread(
        compose_local_build,
        request.model,
        signing_pub=request.signing_pub,
        sdk_sources=tuple(Path(source) for source in request.sdk_sources),
        work_root=_work_root(request, ".mcuhome-local"),
        env=dict(request.env),
        project_root=request.project_root,
        registries=request.registries,
        image=execution.image,
        jobs=request.jobs,
        ccache_dir=execution.ccache_dir,
        context_dir=request.context_dir,
        on_line=request.on_line,
        on_step=request.on_step,
        options=options_for(request),
    )
    outcome = result.outcome
    return BuildOutcome(
        method=LOCAL,
        successful=outcome.successful,
        status=outcome.status,
        context_id=outcome.context_id,
        artifacts=tuple(outcome.artifacts),
        out_dir=result.out_dir,
        report=BUILD_REPORT_FILE,
        image=result.image,
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

    Placed exactly where the ``local`` method places its own and written
    by the same function, so the two methods differ in where the context
    goes and in nothing about what it is.

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
        sdk_sources=tuple(Path(source) for source in request.sdk_sources),
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
    )
    if request.on_step is not None:
        request.on_step(
            "environment",
            build_environment=context_facts(context_dir)["build_environment"],
            zephyr="",
            found_under="",
            fetched=False,
        )
    return context_dir


async def _run_remote(request: BuildRequest, target: RemoteBuild) -> BuildOutcome:
    """The build-server method: :func:`…sessionclient.run_remote_build`.

    The session client is imported here rather than at module level so
    that importing this package costs neither its weight nor the
    ``remote`` extra: ``aiohttp`` and ``zstandard`` are refused in words
    the first time a frame would be sent, not at import.

    Two things are refused before that, because neither can be invented.
    **The server address** is E53's ladder and its rungs belong to the
    caller — a build server has no default, and a wrong one is a build
    context sent to a stranger. **An SDK source** is E65's: the context
    this method creates carries the SDK package's version *and* its
    sha256, and both come from the client's own source directories
    (later, from a registry index). The version is the key the server
    resolves the package by; the hash is what it verifies the bytes it
    found against, which is what makes "same version, other bytes" a
    typed refusal there instead of a silent build against another SDK.
    A client that states no source can resolve neither, and a context
    pinned to whatever the server happened to have would be an identity
    that describes nothing.

    With both in hand this is the ``local`` method's own composition with
    a socket in place of a container: resolve the pin, write the base
    context through the seam both methods share
    (:func:`~mcuhome.workbench.contextdir.create_build_context`), and
    hand the directory to the session client. It is the **base** context
    that goes — unlocked, without a ``manifest.yaml`` — because freezing
    it is the server's act (ADR 0019, E7) and the client's duty is to
    compare the identity it answers with (E37). An embedder that already
    holds a context passes it as :attr:`BuildRequest.context_dir` and
    none of this runs.
    """
    if not target.server:
        raise RemoteNotConfigured(
            "The remote build method needs the address of a build server, and none is set.",
            hint=(
                "configure a builder once, or name the server outright:\n"
                "    builders:                    # mcuhome.yaml, or your user/system\n"
                "      - name: attic              # configuration.yaml\n"
                "        type: remote\n"
                "        server: <host[:port]>\n"
                "    with its token in secrets/build-server/attic.yaml, selected via\n"
                "    --builder attic or once via default_builder;\n"
                "or fully manually:\n"
                "    --build-mode remote --build-server <host[:port]> [--build-token <token>]\n"
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
        if not request.sdk_sources:
            raise RemoteNotConfigured(
                "The remote build method needs an SDK source to pin the build context "
                "with, and none is configured.",
                hint=(
                    "point at a directory holding an MCUHome SDK package:\n"
                    "    mcuhome config set build.sdk_sources <dir> --user\n"
                    "or pass --sdk-sources <dir> for a single build."
                ),
            )
        if request.on_step is not None:
            request.on_step("context")
        # Off the event loop: this hashes nothing large, but it reads an
        # index, writes the model and the key, and copies the patch set —
        # filesystem work with no await in it, in a method whose whole
        # point is that a caller's loop keeps running while it waits.
        context_dir = await asyncio.to_thread(_remote_context, request, work_root)
        if request.on_step is not None:
            # No `id` among these: freezing the context is the server's
            # act (E37), so a base context on its way out has none yet.
            request.on_step("context", **context_facts(context_dir))

    from mcuhome.workbench import sessionclient

    if request.on_step is not None:
        request.on_step("compile", server=target.server)
    result = await sessionclient.run_remote_build(
        context_dir,
        url=url,
        token=target.token,
        work_root=work_root,
        mode=request.mode,
        on_line=request.on_line,
        on_wait=request.on_wait,
        wait=target.wait,
        max_wait=target.max_wait_seconds,
    )
    return BuildOutcome(
        method=REMOTE,
        successful=result.successful,
        status=result.status,
        context_id=result.context_id,
        artifacts=tuple(result.artifacts),
        out_dir=result.out,
        report=BUILD_REPORT_FILE,
        detail=result,
    )
