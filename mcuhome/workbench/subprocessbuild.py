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
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mcuhome.model import containerpaths
from mcuhome.model.context import MANIFEST_FILE
from mcuhome.model.jobs import JOBS_VAR

from mcuhome.workbench.buildenvsession import (
    ACTION_BUILD,
    BASE_DIR_VAR,
    CCACHE_SUBDIR,
    ENTRY_POINT,
    BuilderSession,
    CacheTier,
    Launcher,
    Step,
)
from mcuhome.workbench.buildenvstore import (
    TOOLS_KIND,
    WORKSPACE_KIND,
    BuildEnvironmentError,
    StoreEntry,
    entry_directory,
    git_config_file,
    provisioned,
)
from mcuhome.workbench.contextdir import read_context_manifest
from mcuhome.workbench.orchestrator import (
    LineSink,
    LocalOutcome,
    Running,
    acquire_sdk,
    spawn_process,
)
from mcuhome.workbench.packageregistry import RegistrySource

__all__ = [
    "ENTRY_POINT_DIR",
    "TOOLS_ROOT_VAR",
    "WORKSPACE_ROOT_VAR",
    "Environment",
    "SubprocessBuildResult",
    "cache_tiers",
    "entry_point_of",
    "environment_from_store",
    "launcher",
    "run_locked_build",
    "step_environment",
]

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


@dataclass(frozen=True)
class Environment:
    """The store entries one subprocess build runs against.

    Two packages, and the entry point is in the tools one because the
    interpreter it hands over to lives there too. Both are frozen store
    entries: this profile reads them and never writes into either.
    """

    workspace: StoreEntry
    tools: StoreEntry

    @property
    def entry_point(self) -> Path:
        return entry_point_of(self.tools)

    def described(self) -> str:
        """The package set, for a log line and for a build's own record."""
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
    """
    tools = environment.tools.path
    workspace = environment.workspace.path
    values = {
        "PATH": env.get("PATH") or DEFAULT_PATH,
        "HOME": env.get("HOME") or str(step.session.home_dir),
        BASE_DIR_VAR: str(step.base_dir),
        TOOLS_ROOT_VAR: str(tools),
        WORKSPACE_ROOT_VAR: str(workspace),
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


def cache_tiers(
    *,
    ccache_dir: Path | None = None,
    shared_ccache_dir: Path | None = None,
    session_dir: Path | None = None,
    project_dir: Path | None = None,
) -> dict[str, CacheTier]:
    """The cache tiers this orchestrator provides a step, from directories.

    **The ``local`` tier is where the durable cache goes**, and that is
    not a contradiction of the specification's "it is per step": a tier
    is only per step if the orchestrator leaves it that way, and the
    specification says in the same paragraph that the orchestrator "may
    or may not mount something over it". MCUHome's own environment uses
    the most local writable tier as its primary cache, which is the
    local one — so an orchestrator that wants a compiler cache in this
    profile at all provides a durable directory for it, exactly as the
    container profile mounts a host directory at the same place.

    The two directory names under a cache root are the container
    profile's own, so that one cache root serves both profiles rather
    than each inventing a layout. The *entries* in them are not shared:
    the container profile points ccache at the role directory itself and
    this one at ``<tier>/ccache`` inside it, and the compile commands
    differ by their paths anyway.
    """
    tiers: dict[str, CacheTier] = {}
    if ccache_dir is not None:
        tiers["local"] = CacheTier(
            path=Path(ccache_dir) / containerpaths.CCACHE_LOCAL.name, writable=True
        )
    if session_dir is not None:
        tiers["session"] = CacheTier(path=Path(session_dir), writable=True)
    if project_dir is not None:
        tiers["project"] = CacheTier(path=Path(project_dir), writable=True)
    shared = shared_ccache_dir
    if shared is None and ccache_dir is not None:
        shared = Path(ccache_dir) / containerpaths.CCACHE_SHARED.name
    if shared is not None and Path(shared).is_dir():
        tiers["shared"] = CacheTier(path=Path(shared), writable=False)
    return tiers


# --------------------------------------------------------------------------
# One build, from a locked context
# --------------------------------------------------------------------------


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
    """
    context_dir = Path(context_dir).resolve()
    work_root = Path(work_root).resolve()
    work_root.mkdir(parents=True, exist_ok=True)
    manifest = read_context_manifest(context_dir / MANIFEST_FILE)
    package = acquire_sdk(
        version=manifest.sdk.version,
        sha256=manifest.sdk.sha256,
        sources=tuple(Path(source) for source in sdk_sources),
        into=work_root / "sdk",
        registry=registry,
    )
    session = BuilderSession(
        root=work_root / "session",
        context_dir=context_dir,
        sdk_tree=package.tree,
        entry_point=environment.entry_point,
        launcher=launcher(environment, env=env, jobs=jobs),
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
