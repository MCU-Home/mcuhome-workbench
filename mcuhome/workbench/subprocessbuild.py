# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The subprocess profile: a build environment from the store, run as a process.

The build environment specification has two profiles, and this is the
one for hosts where a container runtime is unavailable — inside an
unprivileged container, for instance — or unwanted. The environment's
packages are unpacked into a per-user store
(:mod:`mcuhome.workbench.buildenvstore`) and frozen read-only there, and
a step is entered by running the entry point out of that store as an
ordinary child process.

**This profile isolates nothing.** The builder runs with the calling
user's rights, on the calling user's filesystem. A build context is
untrusted input — it carries patches, which are code — so a machine that
builds contexts it did not create itself uses the container profile.
Nothing here pretends otherwise; what it does provide is the *pristine*
guarantee the specification does demand: the store is read-only and
every step gets fresh directories, which together are this profile's
implementation of "the environment's source trees are pristine at the
start of every step".

**Everything about the boundary lives one module over.** The tree, the
request document, the result document and the judging are
:mod:`mcuhome.workbench.buildenvsession`'s and are the same in both
profiles. What is here is the profile: which store entries are used,
what environment the child process is given, and how the log gets out.

**The store is never written.** Not by this module, not by the child:
the entries are frozen, a patched tree becomes a copy under ``work``,
and the compiler cache lives in a cache tier the orchestrator provides.
The one write this profile does outside its own session directory is the
one ccache does inside the tier it was pointed at.

**Development mode** is the same profile with a different *source* of
the environment: a west workspace the developer maintains
(:func:`environment_from_workspace`) instead of two provisioned store
entries. Everything after that is the same machinery — the same session
tree, the same request document, the same builder, the same view under
``work`` — and the differences are exactly three. The SDK is the
workspace's own manifest repository, delivered at ``mcuhome/sdk`` like
any other; the tools are whatever is on the ``PATH`` the build was
started from, so the child is the developer's own interpreter running
the builder out of their checkout rather than an entry point out of a
package; and nothing verified any of those bytes, so nothing is checked
against a declaration, an interpreter or a package manifest.

The one thing that mode cannot do is take a build context's patches —
applying them would edit somebody's own source trees and ignoring them
would build firmware that is not what the context says. It is refused
instead.

**Nothing is ever written into that workspace.** What the builder needs
and a checked-out workspace does not carry — the two documents saying
where its west workspace and its layers are — is written into the
session, pointing at the workspace from outside
(:mod:`mcuhome.workbench.devworkspace`).
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mcuhome.model.buildenvironment import (
    DECLARATION_FILE,
    SPEC_GENERATION,
    TOOLS_SOURCE,
    WORKSPACE_SOURCE,
    Declaration,
    family_of,
    member_name,
    parse_declaration,
)
from mcuhome.model.context import (
    BUILD_CONTEXT_FILE,
    MANIFEST_FILE,
    PATCHES_DIR,
    ContextEnvironment,
    DeveloperEnvironment,
    EnvironmentPin,
    PackagePin,
    format_generator_chain,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.jobs import JOBS_VAR

from mcuhome.workbench import devworkspace
from mcuhome.workbench.buildenvsession import (
    ACTION_BUILD,
    BASE_DIR_VAR,
    CCACHE_SUBDIR,
    ENTRY_POINT,
    BuilderSession,
    CacheTier,
    Launcher,
    LocalOutcome,
    Step,
    cache_tiers,
)
from mcuhome.workbench.buildenvstore import (
    TOOLS_KIND,
    WORKSPACE_KIND,
    BuildEnvironmentError,
    StoreEntry,
    entry_directory,
    git_config_file,
    provision,
    provisioned,
)
from mcuhome.workbench.buildprocess import LineSink, Running, spawn_process
from mcuhome.workbench.contextdir import read_context_manifest, read_generator_chain
from mcuhome.workbench.packagefetch import acquire_sdk
from mcuhome.workbench.packageregistry import RegistrySource
from mcuhome.workbench.resolve_pins import concrete_package

__all__ = [
    "DEV_WORKSPACE_OPTION",
    "ENTRY_POINT_DIR",
    "TOOLS_ROOT_VAR",
    "WORKSPACE_ROOT_VAR",
    "Environment",
    "SubprocessBuildResult",
    "check_environment",
    "declaration_of",
    "developer_launcher",
    "developer_step_environment",
    "entry_point_of",
    "environment_from_pins",
    "environment_from_store",
    "environment_from_workspace",
    "launcher",
    "refuse_patched_context",
    "run_locked_build",
    "step_environment",
]

#: What points this profile at a west workspace the developer maintains
#: instead of at the store. Named here because every refusal of a
#: development build has to tell a person which setting to change.
DEV_WORKSPACE_OPTION = "build.dev_workspace"

#: Where the entry point sits inside the tools package. Not the
#: specification's business — where an environment keeps its own content
#: is deliberately its own affair — but this profile has to link the
#: file into §4's tree, so it has to know where the package put it.
ENTRY_POINT_DIR = "bin"

#: How MCUHome's own environment is told where its two packages are. The
#: entry point can fall back to its own location for the tools root and
#: can only *check* the workspace root, never derive it, so both are
#: stated: a store entry is at a path no image and no package could have
#: guessed.
TOOLS_ROOT_VAR = "MCUHOME_BUILD_ENV_TOOLS"
WORKSPACE_ROOT_VAR = "MCUHOME_BUILD_ENV_WORKSPACE"

#: Where git is pointed at the store's own configuration. The workspace
#: entry carries a file exempting its repositories from git's ownership
#: check (:func:`~mcuhome.workbench.buildenvstore.git_config_file`), and
#: this is how a build reads it without a per-user or system file being
#: written.
GIT_CONFIG_VAR = "GIT_CONFIG_GLOBAL"

#: The compiler cache, as environment variables. Two of them exist in the
#: container profile as image content (``/etc/ccache.conf``) and have no
#: equivalent in a store entry, so this profile states them here or it
#: caches nothing:
#:
#: ``CCACHE_COMPILERCHECK=content``
#:     The compilers come out of a hash-pinned package and never change
#:     without the package changing, while their mtimes do not survive
#:     being unpacked — so the content is the honest identity and the
#:     default (size and mtime) would miss on every fresh store entry.
#: ``CCACHE_IGNOREOPTIONS=-specs=*``
#:     Without it a Zephyr build caches nothing at all: picolibc is
#:     integrated with ``-specs=picolibc.specs``, a bare file name the
#:     linker resolves against the toolchain and ccache against the
#:     working directory, and every compile is rejected as "bad compiler
#:     arguments". Excluding it from the hash is safe for the same reason
#:     as the compiler check: the specs file is part of the toolchain and
#:     the toolchain is part of the pinned package set.
#:
#: The other two are this profile's own, because only here do the build
#: directories move between steps:
#:
#: ``CCACHE_BASEDIR``
#:     The step's base directory, which is the ancestor of every path a
#:     step invents. ccache rewrites absolute paths below it to relative
#:     ones, so the part that differs from step to step — the step's own
#:     name — is exactly the part that is removed from the hash.
#: ``CCACHE_NOHASHDIR=1``
#:     ``hash_dir = false``. Without it a debug build (``-g``) hashes the
#:     working directory and misses every time, which makes
#:     ``CCACHE_BASEDIR`` useless on its own.
CCACHE_COMPILER_CHECK = "content"
CCACHE_IGNORE_OPTIONS = "-specs=*"

#: The job count travels in :data:`mcuhome.model.jobs.JOBS_VAR`, imported
#: rather than restated: it is the variable the builder resolves its
#: parallelism from, and a second spelling of a wire name is a second
#: thing to keep in step. Generation 3 has no field for a job count —
#: "the orchestrator enforces its limits rather than negotiating them" —
#: but this profile has no cgroup to enforce one with, so the number a
#: person asked for reaches the build the one way the builder reads one.

#: What a child gets when the caller's environment names no ``PATH``. The
#: host baseline lives on it — git, the C compiler, make, the device-tree
#: compiler — and the entry point prepends the packaged tools to whatever
#: is here.
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"

#: How a development build enters the builder: the interpreter on the
#: person's own ``PATH``, running the module the SDK checkout carries.
#: There is no entry point in that form — the entry point is the tools
#: package's way of setting up an environment this build already has
#: (:func:`developer_launcher`).
BUILDER_INTERPRETER = "python3"
BUILDER_MODULE = "mcuhome.compiler.abi"


@dataclass(frozen=True)
class Environment:
    """What one subprocess build runs against, in either of its two forms.

    Ordinarily two frozen store entries: the workspace package carrying
    the source world and the tools package carrying the toolchain, the
    entry point among it because the interpreter it hands over to lives
    there too. This profile reads both and writes into neither.

    :attr:`tools` is ``None`` in the other form, and that absence *is*
    the form: a **development build** runs against a west workspace the
    developer maintains, with whatever tools are on the ``PATH`` it was
    started from. There is no tools package, so there is no entry point
    to run and no package manifest to check — the builder is started
    directly out of the SDK the workspace carries
    (:func:`developer_launcher`). Nobody published those bytes, so
    nothing has a hash and a build against them is reproducible by nobody
    but the person who made them. That is the point of the form and also
    its one hard consequence, which :func:`run_locked_build` enforces: a
    build context that carries patches is refused rather than applied to
    somebody's own trees.
    """

    #: The unpacked workspace package — or, for a development build, the
    #: west workspace itself, with an empty name, version and hash
    #: because nothing published it.
    workspace: StoreEntry
    #: The unpacked tools package, or ``None`` for a development build.
    #: **Stated always**, with no default, because it is what decides
    #: which of the two forms this is: a caller that forgot it would
    #: otherwise have assembled a development build by omission, and a
    #: development build skips every check there is.
    tools: StoreEntry | None
    #: Development build: the workspace's manifest repository, which is
    #: the SDK this build compiles and delivers at ``mcuhome/sdk``.
    sdk: Path | None = None
    #: What ``MCUHOME_BUILD_ENV_WORKSPACE`` names. ``None`` is the
    #: workspace entry itself, which is what a package is; a development
    #: build points it at the description written into the session
    #: (:mod:`mcuhome.workbench.devworkspace`).
    package_root: Path | None = None

    @property
    def developer(self) -> bool:
        """Whether this is a workspace the developer maintains.

        Derived from the absence of a tools package rather than carried
        beside it: the two could otherwise disagree, and every branch
        that reads this asks it because there is no package to work with.
        """
        return self.tools is None

    @property
    def entry_point(self) -> Path | None:
        """The entry point to place at the specification's path, if any.

        ``None`` for a development build: the entry point is content of
        the tools package — it puts that package's virtual environment,
        CMake, Ninja and the Zephyr SDK on ``PATH`` — and a development
        build has none of that to set up. Its tools are the ones the
        person already has.
        """
        return None if self.tools is None else entry_point_of(self.tools)

    @property
    def workspace_root(self) -> Path:
        """The directory ``MCUHOME_BUILD_ENV_WORKSPACE`` names."""
        return self.package_root if self.package_root is not None else self.workspace.path

    def described(self) -> str:
        """The environment as one line, for a log and for a build's record."""
        if self.tools is None:
            # The path, because a development build has no version to
            # name: two builds a week apart run against the same
            # directory and different bytes, and a line stating anything
            # else would be a line nobody can check afterwards.
            return f"developer build from {self.workspace.path}"
        return (
            f"{self.workspace.name} {self.workspace.version}, "
            f"{self.tools.name} {self.tools.version}"
        )


def entry_point_of(tools: StoreEntry) -> Path:
    """Where the tools entry keeps the entry point."""
    return tools.path / ENTRY_POINT_DIR / ENTRY_POINT


def environment_from_store(
    store: Path,
    *,
    workspace: tuple[str, str],
    tools: tuple[str, str],
) -> Environment:
    """The two provisioned entries this build needs, or a legible refusal.

    *workspace* and *tools* are ``(name, version)`` — the concrete
    package names, architecture suffix and all, because a store entry is
    one package and never a family. Nothing is fetched and nothing is
    unpacked here: provisioning is
    :func:`mcuhome.workbench.buildenvstore.provision`'s, and a build that
    reaches this point with an entry missing is a build whose environment
    was never prepared.
    """
    return Environment(
        workspace=_entry(store, kind=WORKSPACE_KIND, name=workspace[0], version=workspace[1]),
        tools=_entry(store, kind=TOOLS_KIND, name=tools[0], version=tools[1]),
    )


def environment_from_workspace(workspace: Path | str) -> Environment:
    """A build environment out of a west workspace the developer maintains.

    A development build, and the **one** check it makes: that this is a
    west workspace with a manifest repository checked out
    (:func:`mcuhome.workbench.devworkspace.manifest_checkout`). Nothing
    else is verified, and that is a decision rather than an omission —
    everything the store checks is a statement somebody published about
    bytes somebody published, and here nobody published anything. There
    is no declaration to hold the environment to, no package manifest to
    compare, no wheel set whose interpreter has to match, and no hash. A
    tree the developer maintains is theirs, pristine or not.

    The manifest repository **is** the SDK this build compiles: the
    workspace's own checkout, delivered to the session at ``mcuhome/sdk``
    like any other SDK, so a change the person makes in it is what gets
    built. Nothing here writes into any of it — the description the
    builder needs is written into the session instead
    (:func:`mcuhome.workbench.devworkspace.write_environment`), when the
    build actually starts and where it can be thrown away.

    The name, version and hash of the workspace entry are empty on
    purpose: they are what a package states about itself, and this is not
    a package.
    """
    path = Path(workspace).resolve()
    checkout = devworkspace.manifest_checkout(path)
    return Environment(
        workspace=StoreEntry(kind=WORKSPACE_KIND, name="", version="", sha256="", path=path),
        tools=None,
        sdk=checkout,
    )


def environment_from_pins(
    pin: ContextEnvironment,
    *,
    env: dict[str, str],
    workspace_source: str = WORKSPACE_SOURCE,
    tools_source: str = TOOLS_SOURCE,
    sources: Sequence[Path] = (),
    workspace_sources: Sequence[Path] = (),
    tools_sources: Sequence[Path] = (),
    registry: Any = None,
    store: Path | str | None = None,
    platform: str | None = None,
    interpreter: str | Path | None = None,
    bounds: Mapping[str, int] | None = None,
    on_line: LineSink | None = None,
) -> Environment:
    """Provision what a context pins and answer with the two store entries.

    The bridge between a build context and this profile: a context pins
    the environment's packages, and this turns that pin into two frozen
    trees on this machine.

    **A family pin is resolved here and not earlier.** The tools entry of
    a context ordinarily names the family — that is what makes one
    context build the same firmware on hosts of two architectures — and
    the store holds concrete packages, so the family is resolved through
    the index for *this* host, with the pinned hash checked against the
    family's own entry first. A pin that already names one platform's
    package is used as it is, and refuses legibly on a host of another
    platform.

    Provisioning a package that is already in the store costs a marker
    read: :func:`~mcuhome.workbench.buildenvstore.provision` answers
    without touching the network, the disk or its lock.

    *sources* are the operator directories both packages are looked for
    in; *workspace_sources* and *tools_sources* replace them for one
    package each, for a machine that keeps the two large environment
    packages somewhere other than the SDK. *bounds* is how much each
    kind may unpack to, by kind — a kind that is not in it takes the
    store's own bound.
    """
    if isinstance(pin, DeveloperEnvironment):
        # A context of a development build, handed to the store. There is
        # nothing to provision — that context names no packages, on
        # purpose — and the caller has lost track of which build this is,
        # so it is said rather than crashed on.
        raise BuildEnvironmentError(
            "This build context was created for a development build and names no build "
            "environment to unpack.",
            hint=(
                f"build it the way it was created — with {DEV_WORKSPACE_OPTION} pointing "
                "at the workspace it belongs to — or create a context against MCUHome's "
                "own build environment"
            ),
        )
    entries = []
    for package, kind, source, directories in (
        (pin.workspace, WORKSPACE_KIND, workspace_source, workspace_sources),
        (pin.tools, TOOLS_KIND, tools_source, tools_sources),
    ):
        searched = tuple(directories) or tuple(sources)
        found = concrete_package(
            package,
            source=source,
            sources=searched,
            registry=registry,
            platform=platform,
        )
        entries.append(
            provision(
                kind=kind,
                name=found.name,
                version=found.version,
                sha256=found.sha256,
                env=env,
                sources=searched,
                registry=registry,
                store=store,
                platform=platform,
                interpreter=interpreter,
                max_bytes=(bounds or {}).get(kind),
                on_line=on_line,
            )
        )
    workspace, tools = entries
    return Environment(workspace=workspace, tools=_require_entry_point(tools))


def _entry(store: Path, *, kind: str, name: str, version: str) -> StoreEntry:
    """One entry of the store, checked for being the one that was asked for.

    :func:`~mcuhome.workbench.buildenvstore.provisioned` is the only way
    into the store and answers ``None`` for a directory whose completion
    marker is missing — an unpacking that was interrupted, or something
    that was never an environment. Either way it is not one now, and a
    build started against it would fail somewhere deep inside a compile
    instead of here.
    """
    directory = entry_directory(Path(store), name, version)
    found = provisioned(directory)
    if found is None:
        raise BuildEnvironmentError(
            f"The build environment package {name} {version} is not unpacked on this machine.",
            hint=f"MCUHome unpacks it into {directory} before a build starts — run the "
            "build again so it is fetched and unpacked, or build in a container",
        )
    if found.kind != kind:
        raise BuildEnvironmentError(
            f"{directory} holds a {found.kind} package where a {kind} one was expected.",
            hint=f"delete the entry and let MCUHome unpack it again — "
            f"chmod -R u+w {directory} && rm -rf {directory}",
        )
    if kind == TOOLS_KIND:
        _require_entry_point(found)
    return found


def _require_entry_point(tools: StoreEntry) -> StoreEntry:
    """The tools entry carries an entry point this process may run.

    Executable and not merely present, because the difference between
    the two is a build that refuses in a sentence and a build that hangs:
    a program that cannot be started is not a process anybody can wait
    for, so the supervising ladder would run its whole length — the
    deadline first — before saying that nothing ever ran.
    """
    entry_point = entry_point_of(tools)
    if not entry_point.is_file() or not os.access(entry_point, os.X_OK):
        raise BuildEnvironmentError(
            f"The build tools at {tools.path} carry no entry point this machine can run.",
            hint=f"delete the entry and let MCUHome unpack it again — "
            f"chmod -R u+w {tools.path} && rm -rf {tools.path}",
        )
    return tools


# --------------------------------------------------------------------------
# The environment one step is run in
# --------------------------------------------------------------------------


def step_environment(
    step: Step,
    *,
    environment: Environment,
    env: Mapping[str, str],
    jobs: int | None = None,
) -> dict[str, str]:
    """Everything the child process is given, and nothing else.

    A **closed** environment, composed rather than inherited. The
    container profile hands a container what its image and this
    orchestrator state and nothing of the calling shell, and an
    environment that behaved differently in the two profiles would be
    broken rather than clever — so the subprocess profile passes exactly
    the variables below, and a build is never changed by a variable
    somebody exported in the terminal it was started from.

    Two of them are taken from the caller's stated environment because
    they are properties of the *host* rather than of the build:
    ``PATH``, which is where the host baseline lives (the entry point
    prepends the packaged tools to it), and ``HOME``, which several tools
    insist on having — the session provides one when the caller states
    none, exactly as the image provides one for a container run under a
    UID it has no passwd entry for.

    A **development build** is the exact opposite and is composed
    elsewhere (:func:`developer_step_environment`): there the person's
    own environment is the environment, and closing it would throw away
    the tools the build is supposed to use.
    """
    if environment.developer:
        return developer_step_environment(step, environment=environment, env=env, jobs=jobs)
    tools = environment.tools.path if environment.tools is not None else None
    values = {
        "PATH": env.get("PATH") or DEFAULT_PATH,
        "HOME": env.get("HOME") or str(step.session.home_dir),
        BASE_DIR_VAR: str(step.base_dir),
        TOOLS_ROOT_VAR: str(tools),
        WORKSPACE_ROOT_VAR: str(environment.workspace_root),
        GIT_CONFIG_VAR: str(git_config_file(environment.workspace)),
        "CCACHE_BASEDIR": str(step.base_dir),
        "CCACHE_NOHASHDIR": "1",
        "CCACHE_COMPILERCHECK": CCACHE_COMPILER_CHECK,
        "CCACHE_IGNOREOPTIONS": CCACHE_IGNORE_OPTIONS,
    }
    cache = step.writable_cache
    if cache is not None:
        # The most local tier this orchestrator owns. MCUHome's own
        # environment picks its cache out of the tiers itself and this
        # value is then overridden by the one it chose — which is the
        # specification's order, the tiers being the environment's to
        # use. It is stated anyway, for an environment that chooses none:
        # a cache directory is the kind of thing that is better named
        # than left to a default under somebody's home.
        directory = cache / CCACHE_SUBDIR
        directory.mkdir(parents=True, exist_ok=True)
        values["CCACHE_DIR"] = str(directory)
    if jobs is not None and jobs >= 1:
        values[JOBS_VAR] = str(jobs)
    return values


def developer_step_environment(
    step: Step,
    *,
    environment: Environment,
    env: Mapping[str, str],
    jobs: int | None = None,
) -> dict[str, str]:
    """What the builder is given in a development build: the person's own shell.

    An **open** environment, inherited rather than composed, which is the
    reverse of every other build MCUHome runs and is the whole point of
    the mode. The tools are the ones on that ``PATH``: their west, their
    CMake, their Zephyr SDK, their ``ZEPHYR_*``, their ccache
    configuration. A build that closed the environment would be a build
    against tools nobody installed.

    Four things are added, and nothing is taken away except the one
    variable that would be actively wrong:

    ``MCUHOME_BUILDER_BASE_DIR`` and ``MCUHOME_BUILD_ENV_WORKSPACE``
        Where this step's tree is, and where the description of the
        workspace is — the two the builder cannot work out for itself.
    ``PYTHONPATH``
        The SDK checkout in front of whatever was there, because the
        builder that runs is the one in the workspace being developed.
    ``PYTHONDONTWRITEBYTECODE``
        So that importing it leaves no ``__pycache__`` in somebody's
        working tree. See the code below — this is the one write MCUHome
        itself would otherwise make in there.
    the job count
        What the person asked for. It is a request about this build
        rather than a property of their environment, so it is stated
        even here.
    ``MCUHOME_BUILD_ENV_TOOLS`` is **removed** when the caller's
        environment carries one: it names a tools package, this build has
        none, and a value left over from another build would put a
        packaged toolchain in front of the developer's own.
    """
    values = dict(env)
    values.pop(TOOLS_ROOT_VAR, None)
    values[BASE_DIR_VAR] = str(step.base_dir)
    values[WORKSPACE_ROOT_VAR] = str(environment.workspace_root)
    # The one write MCUHome would otherwise make into the workspace, and
    # it is this launcher's own doing: the builder is imported out of the
    # SDK checkout, and CPython caches bytecode next to the source it
    # imports. The checkout is reached through the view as a link, so
    # those files land in the person's own tree — measured, on a real
    # build: 24 `.pyc` files in three `__pycache__` directories under
    # `mcuhome-sdk/`. A build that leaves
    # nothing behind is worth more than the milliseconds a warm cache
    # saves in front of a Zephyr compile.
    values["PYTHONDONTWRITEBYTECODE"] = "1"
    if environment.sdk is not None:
        existing = values.get("PYTHONPATH")
        values["PYTHONPATH"] = (
            f"{environment.sdk}{os.pathsep}{existing}" if existing else str(environment.sdk)
        )
    if jobs is not None and jobs >= 1:
        values[JOBS_VAR] = str(jobs)
    return values


def developer_launcher(
    environment: Environment, *, env: Mapping[str, str], jobs: int | None = None
) -> Launcher:
    """How a step is entered in a development build: the builder, directly.

    No entry point. That file is content of the **tools package** and its
    whole job is to set an environment up — the package's virtual
    environment first on ``PATH``, then its CMake, Ninja, gn and Zephyr
    SDK — which in a development build is the developer's own job and
    already done. What is left of the invocation is the part that is not
    the package's: run ``mcuhome.compiler.abi`` with no arguments, in the
    step's ``work``, with the base directory in the environment. That is
    §6 exactly as the entry point would have handed it over, one process
    earlier.

    The interpreter is the ``python3`` on the ``PATH`` the build was
    started from, resolved here rather than left to the child so that a
    machine without one is a sentence instead of an exec failure inside a
    supervisor's wait. The builder that runs is the SDK checkout's, put
    on ``PYTHONPATH`` by :func:`developer_step_environment` — the same
    thing the entry point does with the SDK the orchestrator delivers.
    """

    def launch(step: Step, on_line: LineSink | None) -> Running:
        values = developer_step_environment(step, environment=environment, env=env, jobs=jobs)
        interpreter = shutil.which(BUILDER_INTERPRETER, path=values.get("PATH"))
        if interpreter is None:
            raise BuildEnvironmentError(
                f"There is no {BUILDER_INTERPRETER} on the PATH this build was started from.",
                hint=(
                    "a development build runs MCUHome's builder with your own Python, "
                    "the way west does — start the build from the shell you develop "
                    f"in, or unset {DEV_WORKSPACE_OPTION} to build against the build "
                    "environment MCUHome unpacks itself"
                ),
            )
        child = spawn_process(
            [interpreter, "-m", BUILDER_MODULE],
            env=values,
            cwd=step.work,
            on_line=on_line,
        )
        if not getattr(child, "started", True):
            raise BuildEnvironmentError(
                f"MCUHome could not start {interpreter} to run the build.",
                hint=(
                    "the interpreter on your PATH cannot be executed — check it with "
                    f"{BUILDER_INTERPRETER} -V"
                ),
            )
        return child

    return launch


def launcher(
    environment: Environment, *, env: Mapping[str, str], jobs: int | None = None
) -> Launcher:
    """How a step is entered in this profile: one child process.

    The entry point is run **by the path the specification fixes** —
    ``mcuhome/bin/build-environment-entry`` below the step's base
    directory, which is the link the session placed into the store's
    copy — and with **no arguments**. The working directory is the step's
    own ``work``: nothing may depend on it, and the one thing a caller
    owes a child is a directory that exists and that it is allowed to be
    in.
    """

    def launch(step: Step, on_line: LineSink | None) -> Running:
        # Checked again here rather than only where the entries were
        # resolved: a caller may have assembled the environment itself,
        # and this is the last moment before a program that cannot start
        # becomes a wait nobody can explain.
        _require_entry_point(environment.tools)
        child = spawn_process(
            [str(step.entry_point)],
            env=step_environment(step, environment=environment, env=env, jobs=jobs),
            cwd=step.work,
            on_line=on_line,
        )
        if not getattr(child, "started", True):
            # A handle to nothing answers "still running" to every
            # question a supervisor asks, so the whole liveness ladder —
            # the deadline first — would run before anybody learned that
            # the program does not exist. The check above catches the
            # ordinary case (a file that is not executable); this catches
            # the rest, a file that cannot be executed at all.
            raise BuildEnvironmentError(
                f"MCUHome could not start the build environment at {step.entry_point}.",
                hint="the entry point is not a program this machine can run — delete the "
                "unpacked build environment and let MCUHome unpack it again",
            )
        return child

    return launch


# --------------------------------------------------------------------------
# One build, from a locked context
# --------------------------------------------------------------------------


def declaration_of(environment: Environment) -> Declaration | None:
    """The environment's own §5 self-description, read off the store.

    The declaration lives at the top of the package that carries it —
    MCUHome's is the architecture-neutral workspace, because a set that
    spans architectures needs a carrier that does not. ``None`` is the
    answer for a **developer** tree that carries none — see below.
    Reading it from the **provisioned entry** is the cheapest verified
    route there is:
    those bytes came out of an archive whose hash was checked against the
    pin, so nothing between the package host and this file could have
    changed what the environment claims. The copy a mirror serves beside
    the archive is the same document, but nothing signs a sidecar, so it
    is not the one a refusal may rest on.
    """
    if environment.developer:
        # A workspace the developer maintains says nothing about itself:
        # a declaration is what a *package* carries, and nobody published
        # these bytes. There is nothing to check against and nothing to
        # complain about — which is why a development build asks none of
        # the questions below.
        return None
    path = environment.workspace.path / DECLARATION_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as missing:
        raise BuildEnvironmentError(
            f"The build environment at {environment.workspace.path} does not say what it is.",
            hint=(
                f"every build environment carries a {DECLARATION_FILE} stating the "
                "specification it implements and the packages it consists of. Delete "
                "the entry and let MCUHome unpack it again."
            ),
        ) from missing
    except ValueError as broken:
        raise BuildEnvironmentError(
            f"The {DECLARATION_FILE} at {environment.workspace.path} is not readable "
            f"JSON: {broken}.",
            hint=f"delete the entry and let MCUHome unpack it again — "
            f"chmod -R u+w {environment.workspace.path} && rm -rf {environment.workspace.path}",
        ) from broken
    return parse_declaration(document, what=str(path))


def check_environment(
    environment: Environment,
    *,
    pin: ContextEnvironment | None = None,
    generator: str = "",
    zephyr_constraint: str = "",
) -> Declaration | None:
    """Everything that has to agree before a step is started, checked at once.

    The build environment specification puts four questions to an
    orchestrator before it starts anything, and until now nothing asked
    any of them — the environment's ``unsupported`` answer was the only
    guard, and it comes after the process has run.

    * **The specification generation** (§12): an orchestrator does not
      start an environment whose generation it does not implement. This
      one implements :data:`~mcuhome.model.buildenvironment.SPEC_GENERATION`.
    * **The generator constraint** (§9.1): the environment declares which
      build contexts it accepts, as a chain of ``<product>:<specifier>``
      entries, and the check runs before *every* step. ``strict`` — the
      default — believes only the leftmost entry of the context's own
      generator chain; ``chain`` walks it and accepts at the first match.
    * **The package set** (§5): what the environment says it consists of
      has to be what the context pinned. A store provisioned from other
      packages than the ones the context names is a different
      environment, whatever it is called.
    * **The Zephyr version**: the device stated a constraint and the
      environment states the release it builds against. This is where the
      two meet — the resolved workspace package's own declaration, out of
      verified bytes, rather than a sidecar nobody signed.

    *pin*, *generator* and *zephyr_constraint* are each optional because
    each is a separate question and a caller may legitimately hold only
    some of them; what is given is checked, what is not is not invented.

    **A development build is exempt from all four**, and not by
    concession: every one of them compares a statement somebody published
    against bytes somebody published, and a west workspace the developer
    checked out is neither. :func:`declaration_of` answers ``None`` for
    it and this answers ``None`` with it.
    """
    declaration = declaration_of(environment)
    if declaration is None:
        return None
    if declaration.spec_generation != SPEC_GENERATION:
        raise BuildEnvironmentError(
            f"The build environment implements build-environment specification "
            f"generation {declaration.spec_generation}, and this MCUHome speaks "
            f"generation {SPEC_GENERATION}.",
            hint=(
                "use a build environment released with this MCUHome, or update "
                "MCUHome to one that speaks the environment's generation"
            ),
        )
    if isinstance(pin, EnvironmentPin):
        _check_packages(declaration, pin, environment)
    if zephyr_constraint:
        _check_zephyr(declaration, zephyr_constraint, environment)
    if generator:
        _check_generator(declaration, generator, environment)
    return declaration


def _check_packages(
    declaration: Declaration, pin: EnvironmentPin, environment: Environment
) -> None:
    """The environment consists of the packages the context pinned.

    Two comparisons, and they answer different questions.

    **The pin against the entry**: is this the package the context named?
    A pin that names the entry outright must name its bytes as well. A pin
    that names the entry's **family** — the normal case for the tools
    package — cannot be compared by hash here, because a family's hash is
    derived from every platform's package and only an index can recompute
    it; that check happened where the family was resolved to this
    platform's package, and what is left to compare is the version.

    **The declaration against the entry**: does the environment agree
    about what it is made of? The declaration is the abstract set — its
    carrier cannot state its own hash and its tools member may name the
    family — so a hash is compared only where the declaration states one.
    """
    for package, entry in ((pin.workspace, environment.workspace), (pin.tools, environment.tools)):
        _check_pinned(package, entry)
        # Two probes, and the order is the specification's: a *delivery*
        # names the concrete package it actually contains, an abstract
        # declaration names the family — and "the abstract declaration
        # matches every delivery of that set". Both are looked up by what
        # the STORE holds, never by what the pin says: the declaration
        # describes the environment, and the pin is what the environment
        # is then held against.
        member = declaration.packages.get(entry.name) or declaration.packages.get(
            family_of(entry.name)
        )
        if member is None:
            named = declaration.described() or "nothing"
            raise BuildEnvironmentError(
                f"The build environment does not consist of {entry.name}; it consists of {named}.",
                hint=(
                    "the build context names the packages its firmware is compiled "
                    "with, and this environment is assembled from others. Build in a "
                    "container, or recreate the context."
                ),
            )
        if member.version != entry.version:
            raise BuildEnvironmentError(
                f"The build environment states {member_name(entry.name)} "
                f"{member.version} and the unpacked package is {entry.version}.",
                hint=f"delete the entry and let MCUHome unpack it again — "
                f"chmod -R u+w {entry.path} && rm -rf {entry.path}",
            )
        if member.sha256 is not None and member.sha256 != entry.sha256:
            raise BuildEnvironmentError(
                f"The build environment states {member_name(entry.name)} at hash "
                f"{member.sha256} and the unpacked package is {entry.sha256}.",
                hint=f"delete the entry and let MCUHome unpack it again — "
                f"chmod -R u+w {entry.path} && rm -rf {entry.path}",
            )


def _check_pinned(package: PackagePin, entry: StoreEntry) -> None:
    """One store entry against the pin it is supposed to be delivering."""
    if package.name not in (entry.name, family_of(entry.name)):
        raise BuildEnvironmentError(
            f"The build context pins {package.name} and the unpacked package is {entry.name}.",
            hint=(
                "the environment this build was prepared with is not the one the "
                "context names. Build in a container, or recreate the context."
            ),
        )
    if package.version != entry.version:
        raise BuildEnvironmentError(
            f"The build context pins {package.name} {package.version} and the "
            f"unpacked package is {entry.version}.",
            hint=(
                "the environment this build was prepared with is not the one the "
                "context names. Build in a container, or recreate the context."
            ),
        )
    if package.name == entry.name and package.sha256 != entry.sha256:
        raise BuildEnvironmentError(
            f"The build context pins {package.name} at hash {package.sha256} and the "
            f"unpacked package is {entry.sha256}.",
            hint=f"delete the entry and let MCUHome unpack it again — "
            f"chmod -R u+w {entry.path} && rm -rf {entry.path}",
        )


def _check_zephyr(declaration: Declaration, constraint: str, environment: Environment) -> None:
    """The environment's Zephyr release satisfies the device's constraint."""
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
        raise BuildEnvironmentError(
            f'The build environment states Zephyr "{declaration.zephyr_version}", '
            "which is not a version.",
            hint=f"the environment at {environment.workspace.path} is damaged — "
            "delete the entry and let MCUHome unpack it again",
        ) from broken
    if not specifier.contains(version, prereleases=True):
        raise BuildEnvironmentError(
            f"This device needs Zephyr {constraint} and the build environment "
            f"builds against {declaration.zephyr_version}.",
            hint=(
                "use a build environment for the Zephyr release the device asks for, "
                "or loosen the device's Zephyr constraint"
            ),
        )


def _check_generator(declaration: Declaration, generator: str, environment: Environment) -> None:
    """The environment accepts a context this generator chain describes (§9.1)."""
    from mcuhome.workbench.generatorconstraint import accepts

    if accepts(
        declaration.generator_constraint,
        generator,
        mode=declaration.generator_constraint_mode,
    ):
        return
    raise BuildEnvironmentError(
        f"The build environment does not accept build contexts from {generator}.",
        hint=(
            f"it accepts {declaration.generator_constraint or 'nothing'}. Use a build "
            "environment released with this MCUHome, or recreate the context with a "
            "matching version."
        ),
    )


def refuse_patched_context(context_dir: Path, environment: Environment) -> None:
    """A build context with patches is refused against a developer's workspace.

    The two ways to be wrong here are both worse than refusing. **Applying**
    the patches would change what is built out of a workspace the developer
    maintains — the whole point of the mode is that the workspace is theirs
    and is built exactly as it stands, patches of their own included.
    **Ignoring** them would produce firmware that does not contain what the
    build context says it contains, silently, and hand it to whoever the
    device goes to.

    So neither. It is called **twice on purpose**: by the composition
    before the context is locked — locking writes into a directory the user
    keeps, and a build that is going to be refused must not have changed
    anything first — and again at the top of :func:`run_locked_build`,
    which is the entry point an embedder or a test reaches directly. The
    check is a directory listing; running it twice costs nothing and makes
    the rule one no caller can go around.

    Against a provisioned store there is nothing to refuse — the trees the
    patches name are copied and patched in the copy, which is what the
    build environment specification asks for and what makes the store come
    out of the build unchanged.
    """

    patches = context_dir / PATCHES_DIR
    if not environment.developer or not patches.is_dir():
        return
    carried = sorted(entry.name for entry in patches.iterdir())
    if not carried:
        return
    raise BuildEnvironmentError(
        f"This build applies patches ({', '.join(carried)}) and cannot apply them to a "
        f"build environment you maintain yourself.",
        hint=f"unset {DEV_WORKSPACE_OPTION} and run the build again — MCUHome then "
        "unpacks its own build environment, patches a copy of the trees the patches "
        "name and leaves your own workspace untouched",
    )


@dataclass(frozen=True)
class SubprocessBuildResult:
    """What one :func:`run_locked_build` produced, from the caller's side.

    The same shape the container profile's result has, minus the image:
    there is none, and the environment is named by its packages instead.
    """

    outcome: LocalOutcome
    out_dir: Path
    context_dir: Path
    environment: Environment


def run_locked_build(
    context_dir: Path,
    *,
    environment: Environment,
    sdk_sources: tuple[Path, ...] | list[Path],
    work_root: Path,
    env: Mapping[str, str],
    jobs: int = 1,
    ccache_dir: Path | None = None,
    tiers: Mapping[str, CacheTier] | None = None,
    sdk_max_bytes: int | None = None,
    registry: RegistrySource | None = None,
    deadline_seconds: int = 5400,
    on_line: LineSink | None = None,
) -> SubprocessBuildResult:
    """Drive one ``build`` step over a locked context, in the subprocess profile.

    The backend role and nothing else: *context_dir* was created and
    locked by the workbench, *sdk_sources* are the operator's local
    package directories the SDK is acquired from and *registry* the tier
    it falls through to, and *work_root* is this backend's own scratch
    area — the session's directories, the SDK it unpacked, and the steps.

    **Which environment is a parameter here**, unlike in the container
    profile, because in this one the environment is not addressable by a
    digest: it is a set of packages the caller resolved and provisioned,
    and what reaches this function is the two store entries that came out
    of it.

    **A development build takes neither of the two inputs above.** Its SDK
    is the workspace's own manifest repository rather than a package, so
    *sdk_sources* and *registry* are not consulted and nothing is fetched;
    what is written is the description of that workspace, into
    *work_root* and never into the workspace
    (:mod:`mcuhome.workbench.devworkspace`).

    **The environment is checked here as well**, for the reason
    :func:`refuse_patched_context` is called here as well: this is the
    entry point an embedder or a test reaches directly, and a rule a
    caller can go around by calling one function lower is not a rule.
    Everything the context can answer on its own is checked — the
    specification generation, the packages the environment consists of
    against the ones the context pinned, and the build contexts the
    environment accepts against this context's generator chain. The
    device's Zephyr constraint is **not** among them: it is a property of
    the device model, which a locked context does not carry, so
    :func:`check_environment` is given it by the composition instead.
    """
    context_dir = Path(context_dir).resolve()
    work_root = Path(work_root).resolve()
    refuse_patched_context(context_dir, environment)
    work_root.mkdir(parents=True, exist_ok=True)
    manifest = read_context_manifest(context_dir / MANIFEST_FILE)
    if environment.developer:
        # Nothing is acquired and nothing is checked. The SDK is the
        # workspace's own manifest repository — that is what the mode
        # means — and it is delivered at `mcuhome/sdk` exactly as an
        # unpacked package would be, so everything below this line runs
        # the same build as every other one. The description the builder
        # reads its trees out of is written here, into the session, and
        # never into the workspace it describes.
        environment = replace(
            environment,
            package_root=devworkspace.write_environment(
                environment.workspace.path, work_root / "environment", env=env
            ),
        )
        sdk_tree = environment.sdk
        launch = developer_launcher(environment, env=env, jobs=jobs)
    else:
        check_environment(
            environment,
            pin=manifest.build_environment,
            generator=format_generator_chain(
                read_generator_chain(context_dir / BUILD_CONTEXT_FILE)
            ),
        )
        sdk_tree = acquire_sdk(
            version=manifest.sdk.version,
            sha256=manifest.sdk.sha256,
            sources=tuple(Path(source) for source in sdk_sources),
            into=work_root / "sdk",
            registry=registry,
            max_bytes=sdk_max_bytes,
        ).tree
        launch = launcher(environment, env=env, jobs=jobs)
    session = BuilderSession(
        root=work_root / "session",
        context_dir=context_dir,
        sdk_tree=sdk_tree,
        entry_point=environment.entry_point,
        launcher=launch,
        context_id=manifest.compute_id(),
        tiers=tiers if tiers is not None else cache_tiers(ccache_dir=ccache_dir),
        deadline_seconds=deadline_seconds,
    )
    with session:
        outcome = session.invoke(ACTION_BUILD, on_line=on_line)
    return SubprocessBuildResult(
        outcome=outcome,
        out_dir=session.out,
        context_dir=context_dir,
        environment=environment,
    )
