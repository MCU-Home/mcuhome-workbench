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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.buildenvironment import (
    SPEC_GENERATION as DECLARED_SPEC_GENERATION,
)
from mcuhome.model.buildenvironment import (
    TOOLS_SOURCE,
    WORKSPACE_SOURCE,
    Declaration,
    PackageMember,
)
from mcuhome.model.buildimage import CCACHE_DIR_VAR, DOCKER_VAR
from mcuhome.model.context import MANIFEST_FILE, ContextEnvironment, DeveloperEnvironment
from mcuhome.model.errors import BuildError, ConfigError
from mcuhome.model.imageref import Reference
from mcuhome.model.jobs import JOBS_VAR
from mcuhome.model.userpaths import expand, home

from mcuhome.workbench.buildenvsession import (
    ACTION_BUILD,
    BASE_DIR_VAR,
    CACHE_TIERS,
    REQUEST_FILE,
    STEP_CACHE,
    STEP_CONTEXT,
    STEP_DIR,
    STEP_OUT,
    STEP_SDK,
    BuilderSession,
    CacheTier,
    EnvironmentUnavailable,
    EnvironmentUnusable,
    Launcher,
    LocalOutcome,
    Step,
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
from mcuhome.workbench.buildtarget import DEFAULT_CONTAINER_REPOSITORIES
from mcuhome.workbench.contextdir import read_context_manifest
from mcuhome.workbench.packagefetch import acquire_sdk
from mcuhome.workbench.packageregistry import RegistrySource
from mcuhome.workbench.resolve_image import (
    ImageMatch,
    image_for_packages,
    parse_image_pin,
)
from mcuhome.workbench.resolve_pins import concrete_package

__all__ = [
    "CONTAINER_REPOSITORIES_OPTION",
    "ContainerBuildResult",
    "Mount",
    "ResourceLimits",
    "Runtime",
    "cache_root",
    "ccache_directory",
    "docker_program",
    "ensure_image",
    "image_for_context",
    "launcher",
    "preflight",
    "prepare_environment",
    "run_locked_build",
    "step_command",
]

#: The configuration key naming the repositories a build environment may
#: be taken from, in search order. Quoted in the refusal that comes when
#: none of them has an image for the pinned package set.
CONTAINER_REPOSITORIES_OPTION = "build.container_repositories"

#: The container program to drive. ``podman`` is command-line compatible
#: for everything used here; it is not tested, hence a variable and not a
#: documented feature.
DEFAULT_RUNTIME = "docker"

#: Where §4's tree is inside the container. ``MCUHOME_BUILDER_BASE_DIR``
#: is ``/`` here, which is what makes every mount target the same string
#: on every machine — and a compiler cache worth having, because Zephyr
#: puts absolute paths into every compile.
BASE_DIR = "/"
_TREE = f"/{STEP_DIR}"
REQUEST_TARGET = f"{_TREE}/{REQUEST_FILE}"
SDK_TARGET = f"{_TREE}/{STEP_SDK}"
CONTEXT_TARGET = f"{_TREE}/{STEP_CONTEXT}"
OUT_TARGET = f"{_TREE}/{STEP_OUT}"
CACHE_TARGET = f"{_TREE}/{STEP_CACHE}"

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
class ResourceLimits:
    """What one step's container may consume, as ``run`` flags.

    §11 tells an environment that "whatever CPU, memory, disk and time
    budget the orchestrator has set, it may enforce hard" — so they go on
    the run that creates the container, where they bound the whole build
    rather than one process in it.

    **Unset is the local default and it is deliberate.** A build on
    somebody's own machine is not a tenant: it gets what the machine has,
    exactly as it did before this profile existed, and a number invented
    here would be a limit nobody chose. An operator that runs other
    people's contexts sets them.
    """

    memory: str | None = None
    cpus: str | None = None
    pids: int | None = None

    def to_arguments(self) -> list[str]:
        argv: list[str] = []
        if self.memory:
            argv += ["--memory", self.memory]
        if self.cpus:
            argv += ["--cpus", self.cpus]
        if self.pids is not None:
            argv += ["--pids-limit", str(self.pids)]
        return argv


class Runtime:
    """The container runtime, as this profile uses it.

    Holds the program name and the two impure operations, resolved at
    call time so that a test which replaced them really did replace them.
    Nothing here knows about contexts, the SDK or the specification: it
    composes an argv and runs it.
    """

    def __init__(
        self,
        program: str = DEFAULT_RUNTIME,
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


def docker_program(env: Mapping[str, str]) -> str:
    """The container program to drive."""
    return env.get(DOCKER_VAR) or DEFAULT_RUNTIME


def preflight(runtime: Runtime, *, env: Mapping[str, str]) -> None:
    """Refuse before the build starts, naming the one thing that is wrong.

    Two failures with two different fixes — no runtime, no daemon — and a
    build that dies ten seconds in with somebody else's error text does
    not tell them apart. A missing *image* is no longer one of them: it
    is fetched (:func:`ensure_image`) rather than complained about.
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


def ensure_image(
    runtime: Runtime,
    reference: Reference,
    *,
    on_line: LineSink | None = None,
) -> bool:
    """Have the resolved image on this machine, fetching it if it is not.

    Answers whether it had to fetch, so a caller can say that out loud —
    a gigabyte-scale download deserves a line of its own rather than a
    silence in the middle of a build. The reference is pinned to a digest
    by the time anything reaches here, which is what makes fetching safe:
    there is exactly one set of bytes that answers to it, and either they
    arrive or the pull fails.

    The pull's own output is the progress report — the runtime writes
    layer counts and percentages, and forwarding them beats inventing a
    spinner over a five-minute silence.
    """
    address = reference.runnable()
    if runtime.present(address):
        return False
    completed = runtime.pull(address, on_line)
    if completed.status is None:
        raise _refuse_no_runtime(runtime.program)
    if completed.status != 0:
        raise BuildError(
            f"MCUHome could not fetch the build environment {address}.",
            hint=(
                "the pull is above this message with the reason. The usual ones are "
                "no network, a registry that needs a login, and a private "
                f"repository:\n    {runtime.program} login "
                f"{reference.registry}\n"
                "mcuhome device build --help shows how to build in another mode."
            ),
        )
    return True


# --------------------------------------------------------------------------
# The compiler cache on this machine
# --------------------------------------------------------------------------


def ccache_directory(env: Mapping[str, str]) -> Path:
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
    override = env.get(CCACHE_DIR_VAR)
    values = dict(env)
    if override:
        return expand(override, values)
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


def cache_root(env: Mapping[str, str], stated: Path | None) -> Path | None:
    """Where this machine keeps its compiler cache, or ``None`` for nowhere.

    The compiler cache belongs to the person building rather than to the
    build directory: it holds the same objects for every device and every
    project, and the working area it used to live in is wiped before each
    build. A caller that resolved a location through the configuration
    layers states it; otherwise the user's cache directory answers.

    **A home directory nobody named is not a refusal here.** A cache is
    an optimization, and a caller with no ``HOME`` — a service, a
    container, a test — is entitled to a build that simply has no cache.
    """
    if stated:
        return Path(stated)
    try:
        return ccache_directory(env)
    except ConfigError:
        return None


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
        Mount(source=step.out, target=OUT_TARGET),
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
    image: str,
    step: Step,
    name: str,
    user: str | None = None,
    limits: ResourceLimits | None = None,
    jobs: int | None = None,
    labels: Mapping[str, str] | None = None,
) -> list[str]:
    """The ``run`` that is one step of the session.

    * **No command and no arguments.** §6 runs the entry point at the
      path it fixes, with no arguments, and the image's own ``CMD`` is
      that entry point — so this argv ends at the image. Overriding it
      would be this side deciding how the environment starts itself.
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
      defines. Everything else in the container's environment is the
      image's own, which is where ``PATH`` and ``HOME`` come from.
    """
    argv = [program, "run", "--rm", "--init", "--network", "none", "--name", name]
    if user is not None:
        argv += ["--user", user]
    argv += ["--env", f"{BASE_DIR_VAR}={BASE_DIR}"]
    if jobs is not None and jobs >= 1:
        # MCUHome's own environment resolves its parallelism from this
        # variable, exactly as it does in the subprocess profile; an
        # environment that does not know the name ignores it, which is
        # the same rule §6.1 gives for a request field.
        argv += ["--env", f"{JOBS_VAR}={jobs}"]
    for label, value in sorted((labels or {}).items()):
        argv += ["--label", f"{label}={value}"]
    argv += (limits or ResourceLimits()).to_arguments()
    for mount in _ordered(step_mounts(step)):
        argv += ["--volume", mount.to_argument()]
    argv.append(image)
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

    def __init__(self, child: Running, *, runtime: Runtime, name: str) -> None:
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
    image: str,
    *,
    runtime: Runtime,
    user: str | None = None,
    limits: ResourceLimits | None = None,
    jobs: int | None = None,
    labels: Mapping[str, str] | None = None,
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
            image=image,
            step=step,
            name=name,
            user=user,
            limits=limits,
            jobs=jobs,
            labels=labels,
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

    match: ImageMatch
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
) -> ImageMatch:
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

    *image_pin* is the one-invocation override, in any of the four forms
    :func:`~mcuhome.workbench.resolve_image.parse_image_pin` reads. It
    narrows which images are looked at; the labels are checked either
    way.
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
    return image_for_packages(
        wanted,
        registry=images,
        repositories=tuple(repositories),
        pin=parse_image_pin(image_pin),
    )


def check_image(
    declaration: Declaration,
    *,
    reference: str,
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
            f"The build environment {reference} implements build-environment "
            f"specification generation {declaration.spec_generation}, and this MCUHome "
            f"speaks generation {DECLARED_SPEC_GENERATION}.",
            hint=(
                "use a build environment released with this MCUHome, or update "
                "MCUHome to one that speaks the environment's generation"
            ),
        )
    if zephyr_constraint:
        _check_zephyr(declaration, zephyr_constraint, reference)
    if generator:
        _check_generator(declaration, generator, reference)


def _check_zephyr(declaration: Declaration, constraint: str, reference: str) -> None:
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
            f'The build environment {reference} states Zephyr "{declaration.zephyr_version}", '
            "which is not a version.",
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


def _check_generator(declaration: Declaration, generator: str, reference: str) -> None:
    from mcuhome.workbench.generatorconstraint import accepts

    if accepts(
        declaration.generator_constraint,
        generator,
        mode=declaration.generator_constraint_mode,
    ):
        return
    raise EnvironmentUnusable(
        f"The build environment {reference} does not accept build contexts from {generator}.",
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
    runtime: Runtime | None = None,
    on_line: LineSink | None = None,
) -> ResolvedImage:
    """From "these packages" to "these bytes, here" — before anything is built.

    Three steps, in the order that makes each refusal cheap. Is there a
    container runtime at all (two refusals with two different fixes).
    Which image declares exactly the package set this context pins (a
    registry question, answered without pulling anything). And finally:
    is it on this machine, or does it have to be fetched.
    """
    seam = runtime if runtime is not None else Runtime(docker_program(env))
    preflight(seam, env=env)
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
    fetched = ensure_image(seam, match.reference, on_line=on_line)
    return ResolvedImage(match=match, fetched=fetched)


# --------------------------------------------------------------------------
# One build, from a locked context
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerBuildResult:
    """What one :func:`run_locked_build` produced, from the caller's side.

    The same shape the subprocess profile's result has, plus the image:
    there the environment is named by its packages, here by the bytes
    that delivered them.
    """

    outcome: LocalOutcome
    out_dir: Path
    context_dir: Path
    image: str


def run_locked_build(
    context_dir: Path,
    *,
    image: ResolvedImage | str,
    sdk_sources: Sequence[Path],
    work_root: Path,
    env: Mapping[str, str],
    jobs: int = 1,
    tiers: Mapping[str, CacheTier] | None = None,
    sdk_max_bytes: int | None = None,
    registry: RegistrySource | None = None,
    deadline_seconds: int = 5400,
    limits: ResourceLimits | None = None,
    labels: Mapping[str, str] | None = None,
    user: str | None = None,
    runtime: Runtime | None = None,
    on_line: LineSink | None = None,
) -> ContainerBuildResult:
    """Drive one ``build`` step over a locked context, in the container profile.

    The backend role and nothing else: *context_dir* was created and
    locked by the workbench, *sdk_sources* are the operator's local
    package directories the SDK is acquired from and *registry* the tier
    it falls through to, and *work_root* is this backend's own scratch
    area — the session's directories and the SDK it unpacked.

    *image* is what runs, pinned to a digest and already resolved by
    whoever composed the build (:func:`prepare_environment`). It is a
    parameter rather than something read back out of the context,
    because a context pins the environment's **packages** and an image is
    one delivery of that set: the party that chose the delivery is the
    party that hands it over. A :class:`ResolvedImage` is **run** by its
    digest and **recorded** in its full form, tag included — the tag is
    documentation for whoever reads the record a year later and is never
    what the runtime resolves.

    The containers this session started are swept when it ends. ``--rm``
    already removed the ones that finished; the sweep is for a step that
    was stopped, and it is best effort because a failed teardown must not
    replace the build's own verdict.
    """
    context_dir = Path(context_dir).resolve()
    work_root = Path(work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    manifest = read_context_manifest(context_dir / MANIFEST_FILE)
    running = image.runnable if isinstance(image, ResolvedImage) else image
    recorded = image.reference if isinstance(image, ResolvedImage) else image
    seam = runtime if runtime is not None else Runtime(docker_program(env))
    sdk_tree = acquire_sdk(
        version=manifest.sdk.version,
        sha256=manifest.sdk.sha256,
        sources=tuple(Path(source) for source in sdk_sources),
        into=work_root / "sdk",
        registry=registry,
        max_bytes=sdk_max_bytes,
    ).tree
    started: list[str] = []
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
            limits=limits,
            jobs=jobs,
            labels=labels,
            started=started,
        ),
        context_id=manifest.compute_id(),
        tiers=tiers,
        deadline_seconds=deadline_seconds,
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
        out_dir=session.out,
        context_dir=context_dir,
        image=recorded,
    )
