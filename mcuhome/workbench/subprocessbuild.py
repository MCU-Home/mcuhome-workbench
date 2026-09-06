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

**Development mode** is the same profile pointed at trees a developer
maintains instead of at store entries
(:func:`environment_from_paths`): a workspace they patch by hand, a
tools tree they rebuilt. Nothing verified those bytes and nothing froze
them, so the one thing that mode cannot do is take a build context's
patches — applying them would edit somebody's own source trees and
ignoring them would build firmware that is not what the context says.
It is refused instead.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model import containerpaths
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
    EnvironmentPin,
    PackagePin,
    format_generator_chain,
)
from mcuhome.model.errors import BuildError, ConfigError
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
    TOOLS_MANIFEST,
    WORKSPACE_KIND,
    WORKSPACE_MANIFEST,
    BuildEnvironmentError,
    StoreEntry,
    entry_directory,
    git_config_file,
    provision,
    provisioned,
    require_manifest,
)
from mcuhome.workbench.contextdir import read_context_manifest, read_generator_chain
from mcuhome.workbench.orchestrator import (
    LineSink,
    LocalOutcome,
    Running,
    acquire_sdk,
    spawn_process,
)
from mcuhome.workbench.packageregistry import RegistrySource
from mcuhome.workbench.resolve_pins import concrete_package

__all__ = [
    "DEV_OPTIONS",
    "DEV_TOOLS_OPTION",
    "DEV_WORKSPACE_OPTION",
    "SHARED_CACHE_OPTION",
    "ENTRY_POINT_DIR",
    "TOOLS_ROOT_VAR",
    "WORKSPACE_ROOT_VAR",
    "Environment",
    "SubprocessBuildResult",
    "cache_tiers",
    "check_environment",
    "declaration_of",
    "entry_point_of",
    "environment_from_paths",
    "environment_from_pins",
    "environment_from_store",
    "launcher",
    "refuse_patched_context",
    "run_locked_build",
    "step_environment",
]

#: What points this profile at trees a developer maintains instead of at
#: store entries. Named here because every refusal of development mode has
#: to tell a person which setting to change; turning them into
#: configuration is a separate piece of work.
#: The configuration key that names a shared compiler cache, quoted in
#: the refusal when the directory it names is not there.
SHARED_CACHE_OPTION = "build.cache_shared"

DEV_WORKSPACE_OPTION = "build.dev_workspace"
DEV_TOOLS_OPTION = "build.dev_tools"
DEV_OPTIONS = {WORKSPACE_KIND: DEV_WORKSPACE_OPTION, TOOLS_KIND: DEV_TOOLS_OPTION}

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
    """The two package trees one subprocess build runs against.

    Two packages, and the entry point is in the tools one because the
    interpreter it hands over to lives there too. Ordinarily both are
    frozen store entries: this profile reads them and never writes into
    either.

    :attr:`developer` says they are not. **Development mode** is a
    developer pointing the build at trees they maintain themselves —
    a workspace they patch, a tools tree they rebuilt — instead of at
    what the store provisioned. The bytes are then nobody's to vouch for:
    they have no package hash, they are writable, and a build against
    them is reproducible by nobody but the person who made them. That is
    the point of the mode and also its one hard consequence, which
    :func:`run_locked_build` enforces: a build context that carries
    patches is refused rather than applied to somebody's own trees.
    """

    workspace: StoreEntry
    tools: StoreEntry
    #: The trees were supplied by a developer rather than provisioned.
    developer: bool = False

    @property
    def entry_point(self) -> Path:
        return entry_point_of(self.tools)

    def described(self) -> str:
        """The package set, for a log line and for a build's own record."""
        described = (
            f"{self.workspace.name} {self.workspace.version}, "
            f"{self.tools.name} {self.tools.version}"
        )
        if not self.developer:
            return described
        # The paths, because in this mode the version is whatever the
        # developer's trees say about themselves and two builds a week
        # apart can state the same one over different bytes. A log line
        # that names only the version would be a log line that cannot be
        # trusted afterwards.
        return f"{described} (developer trees at {self.workspace.path} and {self.tools.path})"


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


def environment_from_paths(workspace: Path | str, tools: Path | str) -> Environment:
    """A build environment out of two directories a developer maintains.

    Development mode. The store's job — fetch, verify, unpack, finalize,
    freeze — is skipped entirely, because there is nothing here that was
    acquired: these are trees the person running the build made. What is
    **not** skipped is every check that can still be made, and they are
    the store's own, applied where the trees carry the answer:

    * each directory exists and is a directory,
    * each carries the package manifest of its kind — the same file the
      entry point itself refuses to start without, and a different name
      per kind, so a workspace handed in as the tools tree is caught
      here rather than three minutes into a compile,
    * that manifest states which package and version the tree is, so a
      build log can say what it ran against,
    * the tools tree carries an entry point this machine can execute.

    What cannot be checked is a hash: nothing published these bytes. The
    environment is marked :attr:`Environment.developer` for that reason,
    and everything downstream that has to behave differently reads that
    flag rather than guessing from a path.
    """
    return Environment(
        workspace=_tree(Path(workspace), kind=WORKSPACE_KIND, manifest=WORKSPACE_MANIFEST),
        tools=_require_entry_point(_tree(Path(tools), kind=TOOLS_KIND, manifest=TOOLS_MANIFEST)),
        developer=True,
    )


def environment_from_pins(
    pin: EnvironmentPin,
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


def _tree(directory: Path, *, kind: str, manifest: str) -> StoreEntry:
    """One developer-supplied package tree, checked against what it claims.

    ``sha256`` comes out empty and that is the honest value: these bytes
    were never acquired from anywhere, so there is no hash anybody could
    have checked them against.
    """
    if not directory.is_dir():
        raise BuildEnvironmentError(
            f"There is no directory at {directory} to build against.",
            hint=f"point {DEV_OPTIONS[kind]} at an unpacked {kind} tree, or unset it "
            "to build against the build environment MCUHome unpacks itself",
        )
    document = require_manifest(directory, manifest, str(directory))
    name = document.get("package")
    version = document.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise BuildEnvironmentError(
            f"{directory / manifest} does not say which package and version it is.",
            hint="a MCUHome build environment tree states both in its manifest — "
            "rebuild the tree with the packaging scripts of the SDK it belongs to",
        )
    return StoreEntry(kind=kind, name=name, version=version, sha256="", path=directory)


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
    local_dir: Path | None = None,
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

    **A shared tier somebody named has to exist.** The shared cache is
    offered read-only and is the one tier this function will not create,
    so a path that is not a directory would silently mean "no shared
    cache" — and a machine configured to start warm off a network mount
    that failed to appear would build cold for weeks without saying so.
    A *derived* shared directory (the one under the cache root) may be
    absent, because that is not a statement anybody made.
    """
    tiers: dict[str, CacheTier] = {}
    # A tier named outright wins over the layout under the cache root:
    # `ccache_dir` says where this machine keeps its caches, `local_dir`
    # says where this one tier is, and a machine that states both meant
    # the more specific of the two.
    local = (
        local_dir
        if local_dir is not None
        else _under_root(ccache_dir, containerpaths.CCACHE_LOCAL.name)
    )
    if local is not None:
        tiers["local"] = CacheTier(path=Path(local), writable=True)
    if session_dir is not None:
        tiers["session"] = CacheTier(path=Path(session_dir), writable=True)
    if project_dir is not None:
        tiers["project"] = CacheTier(path=Path(project_dir), writable=True)
    if shared_ccache_dir is not None:
        shared = Path(shared_ccache_dir)
        if not shared.is_dir():
            raise ConfigError(
                f"The shared compiler cache {shared} is not a directory.",
                hint=(
                    "the shared cache is read-only to a build, so MCUHome does not "
                    "create it: mount or create the directory, or unset "
                    f"{SHARED_CACHE_OPTION} to build without a shared cache"
                ),
            )
        tiers["shared"] = CacheTier(path=shared, writable=False)
        return tiers
    derived = _under_root(ccache_dir, containerpaths.CCACHE_SHARED.name)
    if derived is not None and derived.is_dir():
        tiers["shared"] = CacheTier(path=derived, writable=False)
    return tiers


def _under_root(root: Path | None, name: str) -> Path | None:
    """A role directory under the cache root, or ``None`` without one."""
    return None if root is None else Path(root) / name


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
    path = environment.workspace.path / DECLARATION_FILE
    if environment.developer and not path.is_file():
        # Development mode, and the developer's trees carry no
        # declaration. There is nothing to check against and nothing to
        # complain about: these bytes were never published, so no
        # statement about them exists for anybody to have made. A tree
        # that *does* carry one — anything unpacked from a real package —
        # is held to it exactly as a store entry is.
        return None
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
    pin: EnvironmentPin | None = None,
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

    **Development mode** is exempt twice over, and only where there is
    genuinely nothing to check. Its trees were never published, so no hash
    can agree with anything and the package check is skipped; and a tree
    the developer assembled by hand carries no declaration at all, so
    there is no statement to hold it to and this answers ``None``. A
    developer tree that *does* carry a declaration — anything unpacked
    from a real package, which is the ordinary case — is checked exactly
    as a store entry is, because a build environment still has to
    implement the specification this side speaks.
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
    if pin is not None and not environment.developer:
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
    """A build context with patches is refused against developer trees.

    The two ways to be wrong here are both worse than refusing. **Applying**
    the patches would edit source trees the developer maintains — the whole
    point of the mode is that those trees are theirs, and a build that
    leaves changes in them has broken the thing the person is working on.
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
        hint=f"unset {DEV_WORKSPACE_OPTION} and {DEV_TOOLS_OPTION} and run the build "
        "again — MCUHome then unpacks its own build environment, patches a copy of the "
        "trees the patches name and leaves your own trees untouched",
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
    check_environment(
        environment,
        pin=manifest.build_environment,
        generator=format_generator_chain(read_generator_chain(context_dir / BUILD_CONTEXT_FILE)),
    )
    package = acquire_sdk(
        version=manifest.sdk.version,
        sha256=manifest.sdk.sha256,
        sources=tuple(Path(source) for source in sdk_sources),
        into=work_root / "sdk",
        registry=registry,
        max_bytes=sdk_max_bytes,
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
