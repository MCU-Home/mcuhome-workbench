# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The container profile: a build environment delivered as an image.

The build environment specification has two profiles and this is the one
a machine with a container runtime uses. The environment arrives as a
container image whose labels declare the package set it was assembled
from (§5.2), and a step is entered by starting **one fresh container**
from it — which is how the specification's pristine-tree guarantee (§3)
is met for free: nothing a step wrote survives the container it ran in.

**Everything about the boundary lives one module over.** The tree, the
request document, the result document and the judging are
:mod:`mcuhome.workbench.buildenvsession`'s and are the same in both
profiles. What is here is the profile: which image runs, how it gets
onto this machine, and what is mounted into the container that runs it.

**The launcher relies on the specification and on nothing else.** §4's
tree is what is mounted — the request document, ``sdk`` and
``build-context`` read-only, ``out`` writable, the cache tiers the
operator provides — with ``MCUHOME_BUILDER_BASE_DIR`` set to ``/``, and
the image's own entry point started with no arguments. Where the image
keeps its packages, which source trees it has, how the builder inside
assembles its view: none of it is knowable from out here and none of it
is assumed. That boundary is why an environment built on something other
than Zephyr can be offered behind the same orchestration later.

**``work`` stays in the container.** §4 wants it empty at the start of
every step and writable throughout; a fresh container gives both, and
mounting anything there would hand the step a directory that outlives
it. The image carries the tree's mount points, this side fills the ones
§4 makes the orchestrator's.

**One network call, and it is the registry's.** Choosing an image asks
the configured repositories which of their images declares the pinned
package set (:mod:`mcuhome.workbench.resolve_image`), and fetches those
exact bytes if this machine does not have them. Everything after that is
local, and the step itself runs with no network at all.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcuhome.model.buildenvironment import (
    SPEC_GENERATION as DECLARED_SPEC_GENERATION,
)
from mcuhome.model.buildenvironment import (
    TOOLS_SOURCE,
    WORKSPACE_SOURCE,
    Declaration,
    PackageMember,
)
from mcuhome.model.context import (
    BUILD_CONTEXT_FILE,
    MANIFEST_FILE,
    ContextEnvironment,
    DeveloperEnvironment,
    format_generator_chain,
)
from mcuhome.model.errors import BuildError, ConfigError
from mcuhome.model.userpaths import expand, home

from mcuhome.workbench.buildenvsession import (
    ACTION_BUILD,
    BASE_DIR_VAR,
    CACHE_TIERS,
    ENTRY_POINT,
    REQUEST_FILE,
    STEP_BIN,
    STEP_CACHE,
    STEP_CONTEXT,
    STEP_DIR,
    STEP_OUT,
    STEP_SDK,
    BuilderSession,
    BuildLimits,
    CacheTier,
    EnvironmentUnavailable,
    EnvironmentUnusable,
    Launcher,
    Step,
    StepResult,
    resolve_host_limits,
)
from mcuhome.workbench.buildprocess import (
    Completed,
    LineSink,
    Runner,
    Running,
    Spawner,
    current_user,
    run_command,
    spawn_process,
)
from mcuhome.workbench.buildtarget import (
    DEFAULT_CONTAINER_PROGRAM,
    DEFAULT_CONTAINER_REPOSITORIES,
)
from mcuhome.workbench.contextdir import read_context_manifest, read_generator_chain
from mcuhome.workbench.packagefetch import acquire_sdk
from mcuhome.workbench.packageregistry import RegistrySource
from mcuhome.workbench.resolve_image import (
    ContainerImageMatch,
    parse_container_image,
    resolve_container_image,
)
from mcuhome.workbench.resolve_pins import concrete_package

if TYPE_CHECKING:  # pragma: no cover - types only
    # The build options are resolved one layer up and only annotated
    # here; importing them at run time would make this profile depend on
    # the module that composes it.
    from mcuhome.workbench.build import BuildOptions


__all__ = [
    "CONTAINER_REPOSITORIES_OPTION",
    "DEFAULT_CONTAINER_PIDS",
    "ENTRY_POINT_PATH",
    "ContainerBuildResult",
    "ContainerLimits",
    "ContainerRuntime",
    "Mount",
    "ResolvedImage",
    "default_cache_root",
    "ensure_container_image",
    "image_for_context",
    "launcher",
    "prepare_environment",
    "require_container_image",
    "require_container_runtime",
    "resolve_cache_root",
    "resolve_container_program",
    "run_locked_build",
    "step_command",
]

#: The configuration key naming the repositories a build environment may
#: be taken from, in search order. Quoted in the refusal that comes when
#: none of them has an image for the pinned package set.
CONTAINER_REPOSITORIES_OPTION = "build.container_repositories"

#: Where §4's tree is inside the container. ``MCUHOME_BUILDER_BASE_DIR``
#: is ``/`` here, which is what makes every mount target the same string
#: on every machine — and a compiler cache worth having, because Zephyr
#: puts absolute paths into every compile.
BASE_DIR = "/"
_TREE = f"/{STEP_DIR}"
#: What §6 runs, once per step and with no arguments. Composed from the
#: base directory and the path the specification fixes, never taken from
#: the image's own ``CMD``: an image is not required to name one, and an
#: environment that did would be telling the orchestrator how to start it
#: — which is exactly the thing §6 puts on this side.
ENTRY_POINT_PATH = f"{_TREE}/{STEP_BIN}/{ENTRY_POINT}"
REQUEST_TARGET = f"{_TREE}/{REQUEST_FILE}"
SDK_TARGET = f"{_TREE}/{STEP_SDK}"
CONTEXT_TARGET = f"{_TREE}/{STEP_CONTEXT}"
OUT_TARGET = f"{_TREE}/{STEP_OUT}"
CACHE_TARGET = f"{_TREE}/{STEP_CACHE}"

#: How many processes one step's container may have. Not a tuning knob:
#: a build spawns compilers, and a build that has spawned four thousand
#: of them is not compiling. It is the bound between "many jobs" and "a
#: fork bomb", and nothing that builds firmware comes near it.
DEFAULT_CONTAINER_PIDS = 4096

#: What every container this profile starts is called: the step's own
#: invocation id, which §6.1 promises is safe in a file name and which
#: the naming rules of every runtime accept as well. It exists so that a
#: cancelled step can be reaped by name rather than by hope.
CONTAINER_PREFIX = "mcuhome-"


# --------------------------------------------------------------------------
# The runtime seam
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Mount:
    """One bind mount, host source to container destination.

    ``read_only`` is the whole of the mode, and it is the strongest means
    this profile has: ``build-context`` and ``sdk`` are read-only to the
    kernel rather than by a promise the environment is asked to keep.
    """

    source: Path
    target: str
    read_only: bool = False

    def to_argument(self) -> str:
        suffix = ":ro" if self.read_only else ""
        return f"{self.source}:{self.target}{suffix}"


@dataclass(frozen=True)
class ContainerLimits:
    """What one step's container may consume, as ``run`` flags.

    **The hard half of the two.** The request document tells the
    environment what it should fit in (§6.1); this is what the runtime
    holds it to, and the two carry the same numbers. The orchestrator
    cannot trust an environment to stay inside a recommendation — it may
    have a bug and run amok — so the guard is outside it, and §11 tells
    the environment plainly that whatever budget was set may be enforced
    hard.

    They are **set** in this profile. A local build gets the machine
    it is running on (:func:`~mcuhome.workbench.buildenvsession.resolve_host_limits`)
    and a process count that no build has a use for exceeding, which
    changes nothing about how a healthy build runs and everything about
    what an unhealthy one can do to the machine around it. The one
    exception is memory on a host whose memory cannot be measured: it is
    left unbounded there, unless ``build.memory`` is configured.
    """

    memory: str | None = None
    cpus: str | None = None
    pids: int | None = None

    @staticmethod
    def from_build_limits(
        limits: BuildLimits, *, pids: int = DEFAULT_CONTAINER_PIDS
    ) -> ContainerLimits:
        """The runtime flags for the limits a step was given.

        A figure that was not stated becomes no flag. It is not written
        as a zero, because zero is this runtime's own spelling for *no
        limit* — a machine whose memory could not be measured would then
        get the flag and none of the bound.
        """
        return ContainerLimits(
            memory=_positive(limits.memory_bytes),
            cpus=None if limits.cpus is None or limits.cpus <= 0 else f"{limits.cpus:g}",
            pids=pids,
        )

    def to_arguments(self) -> list[str]:
        argv: list[str] = []
        if _stated(self.memory):
            argv += ["--memory", str(self.memory)]
        if _stated(self.cpus):
            argv += ["--cpus", str(self.cpus)]
        if self.pids is not None:
            argv += ["--pids-limit", str(self.pids)]
        return argv


def _positive(value: int | None) -> str | None:
    """A byte count as a flag value, or nothing for what was not stated."""
    return None if value is None or value <= 0 else str(value)


def _stated(value: str | None) -> bool:
    """Whether a flag value bounds anything.

    Empty is nothing to say, and so is a plain zero: the runtime reads
    ``--memory 0`` and ``--cpus 0`` as *unlimited*, so emitting one would
    be writing a limit that removes the limit. A value with a unit
    (``8g``) is never zero and is taken as it is.
    """
    if not value:
        return False
    try:
        return float(value) > 0
    except ValueError:
        return True


class ContainerRuntime:
    """The container runtime, as this profile uses it.

    Holds the program name and the two impure operations, resolved at
    call time so that a test which replaced them really did replace them.
    Nothing here knows about contexts, the SDK or the specification: it
    composes an argv and runs it.
    """

    def __init__(
        self,
        program: str = DEFAULT_CONTAINER_PROGRAM,
        *,
        runner: Runner | None = None,
        spawner: Spawner | None = None,
    ) -> None:
        self.program = program
        self._runner = runner
        self._spawner = spawner

    def run(self, argv: Sequence[str], on_line: LineSink | None = None) -> Completed:
        """Any command of the runtime, through this seam.

        Public because the seam is only worth having if it covers *all*
        of it: the checks before a build — is the daemon up, is the image
        here, fetch it — are runtime commands too, and a caller that
        stubbed the class but not those would be running a real pull from
        inside its own test.
        """
        runner = run_command if self._runner is None else self._runner
        return runner(list(argv), on_line)

    def spawn(self, argv: Sequence[str], on_line: LineSink | None = None) -> Running:
        """Start a step's container and hand back a handle to it."""
        if self._spawner is not None:
            return self._spawner(list(argv), on_line)
        return spawn_process(list(argv), on_line=on_line)

    def present(self, reference: str) -> bool:
        """Whether this machine already has those bytes."""
        return self.run([self.program, "image", "inspect", reference]).ok

    def pull(self, reference: str, on_line: LineSink | None = None) -> Completed:
        """Fetch the pinned image, forwarding the runtime's own progress."""
        return self.run([self.program, "pull", reference], on_line)

    def remove(self, container: str) -> None:
        """Reap a container. Never raises: teardown must not become the news."""
        self.run([self.program, "rm", "--force", "--volumes", container])


def require_container_runtime(runtime: ContainerRuntime, *, env: Mapping[str, str]) -> None:
    """Refuse before the build starts, naming the one thing that is wrong.

    Two failures with two different fixes — no runtime, no daemon — and a
    build that dies ten seconds in with somebody else's error text does
    not tell them apart. A missing *image* is no longer one of them: it
    is fetched (:func:`ensure_container_image`) rather than complained about.
    """
    del env  # the program name was resolved from it before the seam was built
    completed = runtime.run([runtime.program, "version", "--format", "{{.Server.Version}}"])
    if completed.status is None:
        raise _refuse_no_runtime(runtime.program)
    if completed.status != 0:
        raise _refuse_no_daemon(runtime.program)


def _refuse_no_runtime(program: str) -> BuildError:
    return BuildError(
        f"MCUHome compiles in a container and cannot find {program} on your PATH.",
        hint=(
            "install Docker — https://docs.docker.com/engine/install/ — and run the "
            "same command again. That is the whole host setup: the compiler, the "
            "build system and the source world live in the build environment image, "
            "not on your machine.\n"
            "Building without a container is a build mode: mcuhome device build --help."
        ),
    )


def _refuse_no_daemon(program: str) -> BuildError:
    return BuildError(
        f"MCUHome found {program}, but cannot talk to the Docker daemon.",
        hint=(
            "start it and run the same command again:\n"
            "    sudo systemctl start docker      # Linux, system service\n"
            "    open -a Docker                   # macOS, Docker Desktop\n"
            f"If {program} only works under sudo, add yourself to the `docker` group "
            "and log in again. Building without a container is a build mode: "
            "mcuhome device build --help."
        ),
    )


def ensure_container_image(
    runtime: ContainerRuntime,
    container_image: str,
    *,
    on_line: LineSink | None = None,
) -> bool:
    """Have the resolved image on this machine, fetching it if it is not.

    Answers whether it had to fetch, so a caller can say that out loud —
    a gigabyte-scale download deserves a line of its own rather than a
    silence in the middle of a build. *container_image* is the address
    the runtime is handed, pinned to a digest by the time anything
    reaches here, which is what makes fetching safe: there is exactly one
    set of bytes that answers to it, and either they arrive or the pull
    fails.

    The pull's own output is the progress report — the runtime writes
    layer counts and percentages, and forwarding them beats inventing a
    spinner over a five-minute silence.
    """
    if runtime.present(container_image):
        return False
    completed = runtime.pull(container_image, on_line)
    if completed.status is None:
        raise _refuse_no_runtime(runtime.program)
    if completed.status != 0:
        # An address a runtime can be handed always begins with the
        # registry it lives in, so the login command can name it.
        registry = container_image.split("/", 1)[0]
        raise BuildError(
            f"MCUHome could not fetch the build environment {container_image}.",
            hint=(
                "the pull is above this message with the reason. The usual ones are "
                "no network, a registry that needs a login, and a private "
                f"repository:\n    {runtime.program} login "
                f"{registry}\n"
                "mcuhome device build --help shows how to build in another mode."
            ),
        )
    return True


# --------------------------------------------------------------------------
# The compiler cache on this machine
# --------------------------------------------------------------------------


def default_cache_root(env: Mapping[str, str]) -> Path:
    """Where the compiler cache lives on the host — the root of the tiers.

    A host directory rather than a named volume, for reasons that decide
    it together: a fresh named volume is created root-owned and a
    container running as the calling user cannot write to it; the shared
    tier is meant to be filled from outside, and there is no way into a
    named volume without starting a container; and the cache has to be
    listable, movable and deletable when the runtime is not running at
    all (the subprocess profile has no runtime in the first place).

    One cache per user, not per project. Its keys are content addresses —
    the preprocessed source, the compiler's own bytes, the command line —
    so two projects share an entry exactly when the compilation is the
    same compilation, and a per-project split would cost the sharing
    while protecting nothing.
    """
    values = dict(env)
    if os.name == "nt":
        # LOCALAPPDATA, not APPDATA: the latter roams, and a five-gigabyte
        # compiler cache has no business being copied to a file server at
        # every logon. An environment that names neither falls through to
        # the POSIX form below, which is wrong on Windows but is a path
        # rather than a crash.
        local = values.get("LOCALAPPDATA")
        if local:
            return expand(local, values) / "mcuhome" / "ccache"
    cache_home = values.get("XDG_CACHE_HOME")
    base = expand(cache_home, values) if cache_home else home(values) / ".cache"
    return base / "mcuhome" / "ccache"


def resolve_cache_root(*, options: BuildOptions, env: Mapping[str, str]) -> Path | None:
    """Where this machine keeps its compiler cache, or ``None`` for nowhere.

    The compiler cache belongs to the person building rather than to the
    build directory: it holds the same objects for every device and every
    project, and the working area it used to live in is wiped before each
    build. ``build.cache_root`` states it; unset, the user's cache
    directory answers — *env* is read for that fallback alone and for no
    option.

    **A home directory nobody named is not a refusal here.** A cache is
    an optimization, and a caller with no ``HOME`` — a service, a
    container, a test — is entitled to a build that simply has no cache.
    """
    if options.cache_root:
        return Path(options.cache_root)
    try:
        return default_cache_root(env)
    except ConfigError:
        return None


def resolve_container_program(*, options: BuildOptions) -> str:
    """The program this machine runs containers with.

    ``build.container_program``, which declares ``docker`` as its
    default — a caller that wants to know what a container build would
    drive, before it drives one, asks here rather than reading the field
    and guessing what an unset one means.
    """
    return options.container_program or DEFAULT_CONTAINER_PROGRAM


# --------------------------------------------------------------------------
# One step, in one container
# --------------------------------------------------------------------------


def step_mounts(step: Step) -> list[Mount]:
    """§4's tree for one step, as bind mounts, and nothing besides.

    Six kinds of entry and each is exactly what the specification says it
    is: the request document and the two trees the orchestrator delivers
    are read-only, ``out`` is the session's one directory and writable,
    and the cache tiers are mounted where the step was given one — the
    writable ones writable, ``shared`` read-only, as §8 has it.

    ``work`` is deliberately absent. It is empty at the start of every
    step because the container is new, it is writable because the image
    made it so, and mounting a host directory there would hand the step
    something that outlives it.

    The entry point is absent for the same kind of reason: it is the
    image's own content at the path §4 fixes, and a mount over it would
    replace the environment with this side's idea of it.
    """
    session = step.session
    mounts = [
        Mount(source=step.request, target=REQUEST_TARGET, read_only=True),
        Mount(source=session.sdk_tree, target=SDK_TARGET, read_only=True),
        Mount(source=session.context_dir, target=CONTEXT_TARGET, read_only=True),
        Mount(source=step.out_dir, target=OUT_TARGET),
    ]
    for name in CACHE_TIERS:
        tier = step.cache.get(name)
        if tier is None:
            continue
        writable = session.tiers.get(name)
        mounts.append(
            Mount(
                source=tier,
                target=f"{CACHE_TARGET}/{name}",
                read_only=writable is not None and not writable.writable,
            )
        )
    return mounts


def step_command(
    *,
    program: str,
    container_image: str,
    step: Step,
    name: str,
    user: str | None = None,
    limits: ContainerLimits | None = None,
) -> list[str]:
    """The ``run`` that is one step of the session.

    * **The entry point, by the path §6 fixes, with no arguments.** It
      is named explicitly rather than left to the image's ``CMD``: the
      specification says where the executable is and that it is run once
      per step, and says nothing about ``CMD`` — an image that declares
      none is conforming, and one that declares something else is not
      the thing to start.
    * ``--rm`` because a step's container is over when the step is: what
      it produced is on the ``out`` mount, and §3 says nothing it wrote
      elsewhere survives.
    * ``--init`` because a build spawns hundreds of short-lived children
      and PID 1 has to reap them.
    * ``--network none`` because §11 tells an environment never to
      require the network, and this is the only way that statement is
      checked rather than asserted.
    * ``--user`` because everything the step writes into ``out`` lands on
      a bind mount this side reads back, and files owned by root in
      somebody's build directory are a bug they cannot delete.
    * ``--name`` so that a step which has to be stopped can be reaped by
      name: signalling the client that started it is not the same as
      ending the build inside.
    * ``MCUHOME_BUILDER_BASE_DIR`` — the one variable the specification
      defines, and the only one this side sets. Everything else in the
      container's environment is the image's own, which is where ``PATH``
      and ``HOME`` come from; what the step should fit in travels in the
      request document, not here.
    * ``--cpus``/``--memory``/``--pids-limit``, on the run that creates
      the container, because a limit anywhere else bounds one process
      tree instead of the build.
    """
    argv = [program, "run", "--rm", "--init", "--network", "none", "--name", name]
    if user is not None:
        argv += ["--user", user]
    argv += ["--env", f"{BASE_DIR_VAR}={BASE_DIR}"]
    argv += (limits or ContainerLimits()).to_arguments()
    for mount in _ordered(step_mounts(step)):
        argv += ["--volume", mount.to_argument()]
    argv += [container_image, ENTRY_POINT_PATH]
    return argv


def _ordered(mounts: Sequence[Mount]) -> tuple[Mount, ...]:
    """Mounts ordered so a nested one wins over its parent.

    A runtime applies bind mounts in the order it is given them, so a
    mount inside another has to come after it or the outer one buries it.
    Nothing in §4's tree nests today; the ordering costs nothing and
    keeps a mount set that *would* nest correct regardless of the order
    it was composed in.
    """
    return tuple(sorted(mounts, key=lambda mount: mount.target.count("/")))


class _StepContainer:
    """A running step, stopped the way a container is stopped.

    The handle the launcher answers with. Signalling the client this
    process started is not what ends a build — the build is inside the
    container — so both rungs of the liveness ladder reach the container
    itself and the client afterwards, which is what makes a cancelled
    step actually stop.
    """

    def __init__(self, child: Running, *, runtime: ContainerRuntime, name: str) -> None:
        self._child = child
        self._runtime = runtime
        self._name = name

    @property
    def started(self) -> bool:
        return bool(getattr(self._child, "started", True))

    @property
    def output(self) -> str:
        return str(getattr(self._child, "output", ""))

    def poll(self) -> int | None:
        return self._child.poll()

    def wait(self) -> int | None:
        return self._child.wait()

    def terminate(self) -> None:
        self._runtime.remove(self._name)
        self._child.terminate()

    def kill(self) -> None:
        self._runtime.remove(self._name)
        self._child.kill()


def launcher(
    container_image: str,
    *,
    runtime: ContainerRuntime,
    user: str | None = None,
    limits: ContainerLimits | None = None,
    started: list[str] | None = None,
) -> Launcher:
    """How a step is entered in this profile: one fresh container.

    The session lays out a tree on the host for every step — a directory
    per step with the request document in it and links to the things a
    step is about. This profile uses what that tree *points at*: the
    request document, the SDK, the context, the session's ``out`` and the
    cache tiers are mounted at §4's paths, and the host tree itself is
    not the container's business.

    *started* collects the names of the containers this launcher created,
    so that the caller can sweep them at the end of a session. ``--rm``
    removes a container that ended on its own; the sweep is for the one
    that did not.
    """

    def launch(step: Step, on_line: LineSink | None) -> Running:
        name = f"{CONTAINER_PREFIX}{step.invocation_id}"
        argv = step_command(
            program=runtime.program,
            container_image=container_image,
            step=step,
            name=name,
            user=user,
            limits=limits,
        )
        if started is not None:
            started.append(name)
        child = runtime.spawn(argv, on_line)
        if not getattr(child, "started", True):
            raise EnvironmentUnavailable(
                f"MCUHome could not start {runtime.program} to run the build.",
                hint=(
                    "the container runtime was there a moment ago and cannot be "
                    "started now — check that it is still running"
                ),
            )
        return _StepContainer(child, runtime=runtime, name=name)

    return launch


# --------------------------------------------------------------------------
# Which image runs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedImage:
    """The image one build runs in, and how it was reached.

    :attr:`match` carries the digest and the declaration read off the
    image's labels; :attr:`fetched` says whether those bytes had to be
    pulled, which is worth a line in the log.
    """

    match: ContainerImageMatch
    fetched: bool = False

    @property
    def reference(self) -> str:
        """The full explicit form — what a build report records."""
        return str(self.match.reference)

    @property
    def runnable(self) -> str:
        """How the runtime is told to run those bytes: by digest."""
        return self.match.reference.runnable()

    @property
    def declaration(self) -> Declaration:
        return self.match.declaration


def image_for_context(
    pin: ContextEnvironment,
    *,
    repositories: Sequence[str] = DEFAULT_CONTAINER_REPOSITORIES,
    image_pin: str | None = None,
    workspace_source: str = WORKSPACE_SOURCE,
    tools_source: str = TOOLS_SOURCE,
    sources: Sequence[Path] = (),
    workspace_sources: Sequence[Path] = (),
    tools_sources: Sequence[Path] = (),
    registry: RegistrySource | None = None,
    images: Any = None,
    platform: str | None = None,
) -> ContainerImageMatch:
    """The image that delivers the package set *pin* names.

    Two steps, and the first is the one that makes the second possible.
    A context pins the tools package by its **family** — that is what
    lets one context build the same firmware on hosts of two
    architectures — while an image contains one platform's package and
    declares it by its concrete name. So the pin is resolved against the
    package index for this host first
    (:func:`~mcuhome.workbench.resolve_pins.concrete_package`, the same
    call the subprocess profile provisions from, and the pinned hash is
    checked against the family's own entry there), and what is then
    looked for is the exact set the resolution produced.

    Nothing is fetched here: the index says which package this host
    needs, and the image is what carries the bytes.

    *image_pin* is what this build asks for, in any of the four forms
    :func:`~mcuhome.workbench.resolve_image.parse_container_image` reads —
    whichever of the device's own ``sources.container_image`` and a
    one-invocation override the caller resolved them to. It narrows
    which images are looked at; the labels are checked either way.
    """
    if isinstance(pin, DeveloperEnvironment):
        raise EnvironmentUnavailable(
            "This build context was created for a development build and names no build "
            "environment a container could deliver.",
            hint=(
                "a development build compiles a workspace you maintain, with your own "
                "tools — build it that way, or create a context against MCUHome's own "
                "build environment"
            ),
        )
    wanted: dict[str, PackageMember] = {}
    for package, source, directories in (
        (pin.workspace, workspace_source, workspace_sources),
        (pin.tools, tools_source, tools_sources),
    ):
        found = concrete_package(
            package,
            source=source,
            sources=tuple(directories) or tuple(sources),
            registry=registry,
            platform=platform,
        )
        wanted[found.name] = PackageMember(
            name=found.name, version=found.version, sha256=found.sha256
        )
    return resolve_container_image(
        wanted,
        registry=images,
        repositories=tuple(repositories),
        pin=parse_container_image(image_pin),
        platform=platform,
    )


def require_container_image(
    declaration: Declaration,
    *,
    container_image: str,
    generator: str = "",
    zephyr_constraint: str = "",
) -> None:
    """What has to agree before a container is started.

    The package set is not among them and does not need to be: an image
    is only a candidate at all because its ``packages.`` labels *are* the
    set the context pinned (§5.2). What is left are the questions §5's
    other members answer, and the specification puts all of them to the
    orchestrator before it starts anything:

    * **The specification generation** (§12): this side does not start an
      environment whose generation it does not implement.
    * **The generator constraint** (§9.1): the environment declares which
      build contexts it accepts, and the check runs before *every* step.
    * **The Zephyr release**: the device stated a constraint and the
      image states the release it builds against.

    Each is optional in the sense that a caller may hold only some of
    them; what is given is checked, what is not is not invented.
    """
    if declaration.spec_generation != DECLARED_SPEC_GENERATION:
        raise EnvironmentUnusable(
            f"The build environment {container_image} implements build-environment "
            f"specification generation {declaration.spec_generation}, and this MCUHome "
            f"speaks generation {DECLARED_SPEC_GENERATION}.",
            hint=(
                "use a build environment released with this MCUHome, or update "
                "MCUHome to one that speaks the environment's generation"
            ),
        )
    if zephyr_constraint:
        _check_zephyr(declaration, zephyr_constraint, container_image)
    if generator:
        _check_generator(declaration, generator, container_image)


def _check_zephyr(declaration: Declaration, constraint: str, container_image: str) -> None:
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version

    try:
        specifier = SpecifierSet(constraint)
    except InvalidSpecifier as broken:
        raise BuildError(
            f'The device asks for Zephyr "{constraint}", which is not a version constraint.',
            hint=(
                'constraints are PEP 440: "~=4.4.0", ">=4.4,<5" or "==4.4.0" — npm-style '
                "carets and tildes are not"
            ),
        ) from broken
    try:
        # SemVer pre-releases (4.5.0-rc.1) are PEP 440's 4.5.0rc1; the
        # parser accepts the spelling the specification uses.
        version = Version(declaration.zephyr_version)
    except InvalidVersion as broken:
        raise EnvironmentUnusable(
            f"The build environment {container_image} states Zephyr "
            f'"{declaration.zephyr_version}", which is not a version.',
            hint="the image's labels are damaged — build against another image",
        ) from broken
    if not specifier.contains(version, prereleases=True):
        raise EnvironmentUnusable(
            f"This device needs Zephyr {constraint} and the build environment builds "
            f"against {declaration.zephyr_version}.",
            hint=(
                "use a build environment for the Zephyr release the device asks for, "
                "or loosen the device's Zephyr constraint"
            ),
        )


def _check_generator(declaration: Declaration, generator: str, container_image: str) -> None:
    from mcuhome.workbench.generatorconstraint import accepts

    if accepts(
        declaration.generator_constraint,
        generator,
        mode=declaration.generator_constraint_mode,
    ):
        return
    raise EnvironmentUnusable(
        f"The build environment {container_image} does not accept build contexts from {generator}.",
        hint=(
            f"it accepts {declaration.generator_constraint or 'nothing'}. Use a build "
            "environment released with this MCUHome, or recreate the context with a "
            "matching version."
        ),
    )


def prepare_environment(
    pin: ContextEnvironment,
    *,
    env: Mapping[str, str],
    repositories: Sequence[str] = DEFAULT_CONTAINER_REPOSITORIES,
    image_pin: str | None = None,
    workspace_source: str = WORKSPACE_SOURCE,
    tools_source: str = TOOLS_SOURCE,
    sources: Sequence[Path] = (),
    workspace_sources: Sequence[Path] = (),
    tools_sources: Sequence[Path] = (),
    registry: RegistrySource | None = None,
    images: Any = None,
    container_program: str = DEFAULT_CONTAINER_PROGRAM,
    runtime: ContainerRuntime | None = None,
    on_line: LineSink | None = None,
) -> ResolvedImage:
    """From "these packages" to "these bytes, here" — before anything is built.

    Three steps, in the order that makes each refusal cheap. Is there a
    container runtime at all (two refusals with two different fixes).
    Which image declares exactly the package set this context pins (a
    registry question, answered without pulling anything). And finally:
    is it on this machine, or does it have to be fetched.
    """
    seam = runtime if runtime is not None else ContainerRuntime(container_program)
    require_container_runtime(seam, env=env)
    match = image_for_context(
        pin,
        repositories=repositories,
        image_pin=image_pin,
        workspace_source=workspace_source,
        tools_source=tools_source,
        sources=sources,
        workspace_sources=workspace_sources,
        tools_sources=tools_sources,
        registry=registry,
        images=images,
    )
    fetched = ensure_container_image(seam, match.reference.runnable(), on_line=on_line)
    return ResolvedImage(match=match, fetched=fetched)


# --------------------------------------------------------------------------
# One build, from a locked context
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerBuildResult:
    """What one :func:`run_locked_build` produced, from the caller's side.

    The same shape the subprocess profile's result has, plus the
    container image: there the environment is named by its packages,
    here by the bytes that delivered them.
    """

    outcome: StepResult
    out_dir: Path
    context_dir: Path
    container_image: str


def run_locked_build(
    context_dir: Path,
    *,
    container_image: ResolvedImage | str,
    sdk_sources: Sequence[Path],
    work_root: Path,
    env: Mapping[str, str],
    tiers: Mapping[str, CacheTier] | None = None,
    sdk_max_bytes: int | None = None,
    registry: RegistrySource | None = None,
    deadline_seconds: int = 5400,
    limits: BuildLimits | None = None,
    pids: int = DEFAULT_CONTAINER_PIDS,
    user: str | None = None,
    zephyr_constraint: str = "",
    container_program: str = DEFAULT_CONTAINER_PROGRAM,
    runtime: ContainerRuntime | None = None,
    on_line: LineSink | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ContainerBuildResult:
    """Drive one ``build`` step over a locked context, in the container profile.

    The backend role and nothing else: *context_dir* was created and
    locked by the workbench, *sdk_sources* are the operator's local
    package directories the SDK is acquired from and *registry* the tier
    it falls through to, and *work_root* is this backend's own scratch
    area — the session's directories and the SDK it unpacked.

    *container_image* is what runs, pinned to a digest and already resolved by
    whoever composed the build (:func:`prepare_environment`). It is a
    parameter rather than something read back out of the context,
    because a context pins the environment's **packages** and an image is
    one delivery of that set: the party that chose the delivery is the
    party that hands it over. A :class:`ResolvedImage` is **run** by its
    digest and **recorded** in its full form, tag included — the tag is
    documentation for whoever reads the record a year later and is never
    what the runtime resolves.

    *limits* is what this step is given: the same numbers are written
    into the request document as the recommendation the environment
    sizes itself from, and set on the container as the hard limits the
    runtime holds it to. ``None`` is this machine as it is
    (:func:`~mcuhome.workbench.buildenvsession.resolve_host_limits`) — a local
    build is not a tenant, and the guard exists against a build
    environment that runs amok rather than against the person who
    started it.

    **What the image declares is checked here as well**
    (:func:`require_container_image`), for the reason every entry point that a caller
    can reach directly checks: this is where an embedder and a build
    server enter, and a rule a caller can go around by calling one
    function lower is not a rule. Everything the context can answer on
    its own is checked — the specification generation the image
    implements and the build contexts it accepts against this context's
    generator chain; the device's Zephyr constraint is a property of the
    device model, which a locked context does not carry, so a caller that
    holds one states it as *zephyr_constraint*. A plain reference instead
    of a :class:`ResolvedImage` carries no declaration and is taken as
    already checked by whoever resolved it.

    *should_stop* is asked while the step runs, on the supervisor's own
    tick. The first ``True`` starts the liveness ladder: the container is
    removed, the client this process started is signalled and then
    killed, and what the step had already written into ``out`` stays
    where it is. The step then failed without a result document, which is
    what a stopped step is — the specification has no cancelled status
    and the side that asked for the stop is the only one that can say
    why. :func:`~mcuhome.workbench.buildprocess.resolve_shutdown_seconds`
    is how long that can take.

    The containers this session started are swept when it ends. ``--rm``
    already removed the ones that finished; the sweep is for a step that
    was stopped, and it is best effort because a failed teardown must not
    replace the build's own verdict.
    """
    context_dir = Path(context_dir).resolve()
    work_root = Path(work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    manifest = read_context_manifest(context_dir / MANIFEST_FILE)
    running = (
        container_image.runnable if isinstance(container_image, ResolvedImage) else container_image
    )
    recorded = (
        container_image.reference if isinstance(container_image, ResolvedImage) else container_image
    )
    if isinstance(container_image, ResolvedImage):
        require_container_image(
            container_image.declaration,
            container_image=recorded,
            generator=format_generator_chain(
                read_generator_chain(context_dir / BUILD_CONTEXT_FILE)
            ),
            zephyr_constraint=zephyr_constraint,
        )
    seam = runtime if runtime is not None else ContainerRuntime(container_program)
    sdk_tree = acquire_sdk(
        version=manifest.sdk.version,
        sha256=manifest.sdk.sha256,
        sources=tuple(Path(source) for source in sdk_sources),
        into=work_root / "sdk",
        registry=registry,
        max_bytes=sdk_max_bytes,
    ).tree
    started: list[str] = []
    given = limits if limits is not None else resolve_host_limits()
    session = BuilderSession(
        root=work_root / "session",
        context_dir=context_dir,
        sdk_tree=sdk_tree,
        # The image carries its own entry point at the path §4 fixes,
        # and linking over it would replace the environment's content
        # with this side's idea of it.
        entry_point=None,
        launcher=launcher(
            running,
            runtime=seam,
            user=user if user is not None else current_user(),
            limits=ContainerLimits.from_build_limits(given, pids=pids),
            started=started,
        ),
        context_id=manifest.compute_id(),
        tiers=tiers,
        limits=given,
        deadline_seconds=deadline_seconds,
        should_stop=should_stop,
    )
    try:
        with session:
            outcome = session.invoke(ACTION_BUILD, on_line=on_line)
    finally:
        for name in started:
            with contextlib.suppress(Exception):
                seam.remove(name)
    return ContainerBuildResult(
        outcome=outcome,
        out_dir=session.out_dir,
        context_dir=context_dir,
        container_image=recorded,
    )
