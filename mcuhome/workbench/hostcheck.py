# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""What a build on this machine would need, examined and reported.

A refusal tells one person one thing at the moment they hit it. This
module answers the other question — *would a build work here at all* —
and answers it as data: one finding per thing examined, each with its own
verdict, what was found, and what to do about it. Nothing here raises
over what it found; a host that cannot build is the answer.

**What is examined follows the build mode**, because the two profiles
need disjoint things from a host. A container build needs a container
runtime and a repository that publishes build environments; it needs
neither a Python interpreter nor the environment store. A subprocess
build needs the store and the interpreter that creates the build's
virtual environment — unless a development workspace is configured, in
which case it needs that workspace and neither of the two. Probing all
of it regardless would tell half the machines in the world to install
Docker they will never run, which is the defect this module exists to
fix.

**Every probe is the one the build itself does**, not a copy of it: the
container runtime is examined through
:func:`~mcuhome.workbench.containerbuild.require_container_runtime` and
reported by catching its refusal, so the words a person reads here and
the words they read when a build stops are the same words.

**What the caller hands over decides what can be examined.** Four of the
checks are about the setup a build runs in rather than about the machine:
the project, the resolved configuration, the builders that configuration
defines, and the permissions of the project's secrets. Each of them needs
something only the caller has — a :class:`~mcuhome.workbench.project.Project`,
a :class:`~mcuhome.workbench.configuration.Settings` — and where it is
not handed over the check is **left out rather than guessed at**: a
finding invented from a configuration this call resolved itself would be
a second resolution beside the one the build uses.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.errors import MCUHomeError
from mcuhome.model.imageref import DOCKER_HUB, parse_reference

from mcuhome.workbench import buildenvstore, devworkspace
from mcuhome.workbench.build import BuildOptions
from mcuhome.workbench.buildenvsession import CACHE_TIERS, CacheTier, resolve_cache_tiers
from mcuhome.workbench.builders import SelectedBuilder
from mcuhome.workbench.buildtarget import MODE_CONTAINER, TARGET_REMOTE
from mcuhome.workbench.configuration import Settings, option, resolve_builder
from mcuhome.workbench.containerbuild import (
    ContainerRuntime,
    require_container_runtime,
    resolve_cache_root,
    resolve_container_program,
)
from mcuhome.workbench.diagnostics import Diagnostic
from mcuhome.workbench.imgtool import find_imgtool
from mcuhome.workbench.ociregistry import ImageRegistry
from mcuhome.workbench.project import Project, require_secret_file
from mcuhome.workbench.projectfile import require_current
from mcuhome.workbench.subprocessbuild import BUILDER_INTERPRETER, DEV_WORKSPACE_OPTION

__all__ = [
    "HOST_CHECKS",
    "CacheUsage",
    "HostCheckResult",
    "HostFinding",
    "check_build_host",
    "read_cache_usage",
]

#: What a finding's :attr:`HostFinding.check` may be. A fixed value set,
#: published because a client switches on it: a renderer that knows them
#: can order them, group them and say more about one than the finding's
#: own words do. Append-only, and each name is the option or the thing
#: that was examined.
HOST_CHECKS = (
    "runtime",
    "image",
    "store",
    "python",
    "workspace",
    "imgtool",
    "cache",
    "project",
    "configuration",
    "builder",
    "secrets",
)

#: The configured values a probe here reads itself, named off the
#: registry rather than spelled a second time: what a finding says is the
#: key a person sets, in the one spelling that exists for it.
CONTAINER_REPOSITORIES_OPTION = option("build.container_repositories").name
ENV_STORE_OPTION = option("build.env_store").name
IMGTOOL_OPTION = option("signing.imgtool").name

#: What the interpreter is asked, in one line: the version that decides
#: whether a build environment's wheels can be installed, and whether
#: ``venv`` is there at all — which on a Debian-like system is a separate
#: package and the one thing missing on an otherwise fine host.
_PYTHON_FACTS = (
    "import importlib.util, sys; "
    "print(sys.version_info[0], sys.version_info[1], "
    "1 if importlib.util.find_spec('venv') else 0)"
)


@dataclass(frozen=True)
class HostFinding:
    """One thing examined, and what a person does about it.

    *check* is one of :data:`HOST_CHECKS`, *detail* what was found in
    the words a person reads, and *hint* the fix where there is one — a
    finding that is `ok` usually carries none.
    """

    check: str
    ok: bool
    detail: str
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The finding document: the verdict first, then what it is about."""
        return {"ok": self.ok, "check": self.check, "detail": self.detail, "hint": self.hint}


@dataclass(frozen=True)
class HostCheckResult:
    """What :func:`check_build_host` found, in the order it looked.

    :attr:`ok` is derived rather than stated: a verdict that could
    disagree with the findings it stands in front of would be a second
    answer to the same question.
    """

    findings: tuple[HostFinding, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every finding is."""
        return all(finding.ok for finding in self.findings)

    def to_dict(self) -> dict[str, Any]:
        """The host check document."""
        return {"ok": self.ok, "findings": [finding.to_dict() for finding in self.findings]}


@dataclass(frozen=True)
class CacheUsage:
    """What one compiler cache tier holds on this machine.

    *tier* is one of :data:`~mcuhome.workbench.buildenvsession.CACHE_TIERS`,
    *path* the directory it is laid out in, and *size* and *files* what is
    in there: the bytes of every regular file below it, and how many there
    are. A tier that has never been written to is `0` and `0` — the
    directory a build would create is not there yet, which is a state and
    not an error.
    """

    tier: str
    path: Path
    #: Bytes, summed over the regular files below :attr:`path`.
    size: int
    files: int

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready, every declared key present."""
        return {
            "tier": self.tier,
            "path": str(self.path),
            "size": self.size,
            "files": self.files,
        }


def read_cache_usage(*, options: BuildOptions, env: Mapping[str, str]) -> tuple[CacheUsage, ...]:
    """What the compiler cache holds, one entry per configured tier.

    :func:`~mcuhome.workbench.buildenvsession.resolve_cache_tiers`
    answers *where* the tiers are, and this answers what is in them — the
    number a person wants when a build takes twenty minutes, or when a
    disk is full. The tiers are the ones a build would be given, in the
    order :data:`~mcuhome.workbench.buildenvsession.CACHE_TIERS` states
    them; a tier this machine does not configure is **absent** rather
    than reported as empty, because "no session cache" and "an empty
    session cache" are different answers.

    It walks directories and raises nothing over what it finds there: a
    directory that is not there yet, a file that vanished between the
    listing and the stat, a subtree this account may not enter — each of
    them contributes nothing and the rest is still counted. A **stated**
    shared tier that is not a directory is reported as empty here rather
    than refused the way a build refuses it: this call reports, and the
    build is where a machine configured to start warm and standing cold
    has to stop.

    Symbolic links are not followed and not counted, so a cache that
    links one entry to another is not measured twice.
    """
    root = resolve_cache_root(options=options, env=dict(env))
    tiers = resolve_cache_tiers(
        cache_root=root,
        local=options.cache_local,
        session=options.cache_session,
        project=options.cache_project,
    )
    if options.cache_shared is not None:
        # Stated outright, so it replaces whatever the layout under the
        # cache root offers — and it is put in here rather than passed
        # to the resolution above, which refuses a stated shared tier
        # that is not there. A reading answers what is there instead.
        tiers["shared"] = CacheTier(path=Path(options.cache_shared), writable=False)
    return tuple(_usage_of(name, tiers[name].path) for name in CACHE_TIERS if name in tiers)


def _usage_of(tier: str, path: Path) -> CacheUsage:
    """The bytes and the file count below *path*, whatever is readable."""
    size = 0
    files = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
            size += entry.stat().st_size
        except OSError:
            continue
        files += 1
    return CacheUsage(tier=tier, path=path, size=size, files=files)


def check_build_host(
    *,
    options: BuildOptions,
    env: Mapping[str, str],
    project: Project | None = None,
    imgtool: str | None = None,
    settings: Settings | None = None,
) -> HostCheckResult:
    """Examine what a build with these *options* needs of this machine.

    The project is examined first — there is one or there is not, and an
    older layout is what refuses every other command — then, where
    *settings* was handed over, the resolved configuration and the
    builders it defines. The container checks run for
    ``build.mode = container`` and the store and interpreter checks for
    ``subprocess``; the signing tool and the compiler cache are examined
    either way, because a build needs them whichever profile runs it;
    and the permissions of the project's secrets close the list.

    *imgtool* is the resolved ``signing.imgtool``, exactly as
    :func:`~mcuhome.workbench.imgtool.plan_signing` takes it: nothing
    under this surface reads a configuration channel of its own, so a
    host whose signing tool is configured has to be told about it or the
    check would report on a program that build never runs.

    *project* is the project a build would run in: what paths inside it
    are reported relative to, whose layout version is examined and whose
    ``secrets/`` permissions are. Without one, the project check says so
    and the secrets check is not reported — there is nothing to look at.

    *settings* is the resolved configuration those two further checks
    examine, and without it neither is reported rather than answered
    from a resolution of this call's own.

    It raises nothing. A probe that could not be run is a finding that
    says so.
    """
    findings: list[HostFinding] = [_project_layout(project)]
    if settings is not None:
        findings.append(_configuration(settings))
        findings.append(_builders(settings, options=options, project=project, env=env))
    if options.mode == MODE_CONTAINER:
        findings.append(_container_runtime(options, env))
        findings.append(_container_image(options))
    elif (workspace := options.dev_workspace) is not None:
        findings.append(_dev_workspace(workspace, project=project))
        findings.append(_developer_python(env))
    else:
        findings.append(_env_store(options, env, project=project))
        findings.append(_python(options, env))
    findings.append(_signing_imgtool(env, imgtool))
    findings.append(_cache_root(options, env, project=project))
    if project is not None:
        findings.append(_secrets(project))
    return HostCheckResult(findings=tuple(findings))


# --------------------------------------------------------------------------
# The setup a build runs in
# --------------------------------------------------------------------------


def _project_layout(project: Project | None) -> HostFinding:
    """Is there a project here, which one, and does this MCUHome speak it?

    A machine without a project is not a broken machine — an embedder
    drives a bare device file and never has one — so the absence is a
    finding that says where a project would come from rather than a
    failure. A project whose layout is older than this MCUHome *is* a
    failure: it is what every other command refuses over, in the words
    that refusal uses.
    """
    if project is None:
        return HostFinding(
            check="project",
            ok=True,
            detail="this is not a project directory",
            hint=(
                "a project holds the devices, the secrets and the signing key a "
                "build uses — mcuhome project init creates one"
            ),
        )
    file = project.file
    if file is None:
        # The stand-in root of a device file that lies outside any
        # project: there is no marker to read a version out of.
        return HostFinding(
            check="project",
            ok=True,
            detail=f"{project.root} stands in for a project; it carries no project marker",
        )
    try:
        require_current(file)
    except MCUHomeError as refusal:
        return _refused("project", refusal)
    identity = f", id {file.short_id}" if file.short_id else ""
    return HostFinding(
        check="project",
        ok=True,
        detail=f"{project.root} (project version {file.version}{identity})",
    )


def _configuration(settings: Settings) -> HostFinding:
    """That the configuration resolved, and what of it is not a default.

    The value of this line is the second half: a machine that behaves
    unexpectedly is usually a machine with a setting somebody forgot,
    and the layer that supplied it is what says where to go and change
    it. The bootstrap option is not in a resolution and is therefore not
    here either.
    """
    entries = settings.to_dict()
    beyond = [
        f"{name} ({entry['origin']})"
        for name, entry in entries.items()
        if entry["origin"] != "default"
    ]
    if not beyond:
        return HostFinding(
            check="configuration",
            ok=True,
            detail=f"resolves; all {len(entries)} options are at their default",
        )
    return HostFinding(
        check="configuration",
        ok=True,
        detail=f"resolves; {len(beyond)} option(s) set beyond the defaults: " + ", ".join(beyond),
    )


def _builders(
    settings: Settings,
    *,
    options: BuildOptions,
    project: Project | None,
    env: Mapping[str, str],
) -> HostFinding:
    """The configured builders, and what a plain build does with them.

    Selection is run rather than described — the same call a build makes
    (:func:`~mcuhome.workbench.configuration.resolve_builder`), so a
    ``build.builder`` naming a builder nobody defined is found here
    instead of at the next build. What that selection reads on the way
    is a remote builder's credentials file, whose permissions are warned
    about like every other secret's; a warning is a finding that is not
    ``ok``, because it is something the person has to go and fix.
    """
    complaints: list[Diagnostic] = []
    try:
        selected = resolve_builder(
            settings, name=None, project=project, env=env, on_warning=complaints.append
        )
    except MCUHomeError as refusal:
        return _refused("builder", refusal)
    defined = settings.value("builder")
    listed = (
        ", ".join(
            f"{builder.name} ({builder.target}, from the {builder.origin} layer)"
            for builder in defined
        )
        if defined
        else "none configured"
    )
    plainly = _what_a_plain_build_does(selected, options)
    if complaints:
        return HostFinding(
            check="builder",
            ok=False,
            detail="\n".join(
                [f"{listed}; {plainly}", *(finding.message for finding in complaints)]
            ),
            hint="\n".join(finding.hint for finding in complaints if finding.hint),
        )
    return HostFinding(check="builder", ok=True, detail=f"{listed}; {plainly}")


def _what_a_plain_build_does(selected: SelectedBuilder, options: BuildOptions) -> str:
    """The one sentence a person runs this check to read.

    Both axes, in the two words the configuration spells them with:
    where a plain ``mcuhome device build`` runs, and — where that is this
    machine — how it executes the work.
    """
    named = "" if selected.builder is None else f"{selected.builder.name} takes it: "
    if selected.target == TARGET_REMOTE:
        where = selected.server or "a build server"
        return f"{named}a plain build runs on {where}"
    how = "in a build container" if options.mode == MODE_CONTAINER else "as a child process"
    return f"{named}a plain build runs on this machine, {how}"


def _secrets(project: Project) -> HostFinding:
    """Whether anything under ``secrets/`` is readable by other users.

    Every file, through the guard every reader of a secrets file runs
    (:func:`~mcuhome.workbench.project.require_secret_file`), so what is
    reported here is what a build would warn about — and a key file
    would refuse over. A project that keeps no secrets yet has nothing
    to examine and says so.
    """
    directory = project.secrets_dir
    shown = _shown(directory, project)
    if not directory.is_dir():
        return HostFinding(
            check="secrets",
            ok=True,
            detail=f"{shown} is not there; this project keeps no secrets yet",
            hint=(
                "the directory is part of a project's layout and is created with it — "
                "mcuhome project init --force restores what is missing"
            ),
        )
    if not os.access(directory, os.R_OK | os.X_OK):
        # Asked rather than walked: the path library swallows the error
        # of a directory it may not enter, so a walk would answer "no
        # secrets in there" for a directory nobody can look into.
        return HostFinding(
            check="secrets",
            ok=False,
            detail=f"{shown} belongs to somebody else and cannot be read",
            hint="the project's secrets live here — the directory is the owner's, at mode 700",
        )
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    if not files:
        return HostFinding(
            check="secrets",
            ok=True,
            detail=f"{shown} holds no secrets yet",
        )
    complaints: list[Diagnostic] = []
    for file in files:
        require_secret_file(file, key_material=False, on_warning=complaints.append)
    if complaints:
        return HostFinding(
            check="secrets",
            ok=False,
            detail="\n".join(finding.message for finding in complaints),
            hint="\n".join(finding.hint for finding in complaints if finding.hint),
        )
    return HostFinding(
        check="secrets",
        ok=True,
        detail=f"{shown} holds {len(files)} file(s), each of them owner-only",
    )


# --------------------------------------------------------------------------
# The container profile
# --------------------------------------------------------------------------


def _container_runtime(options: BuildOptions, env: Mapping[str, str]) -> HostFinding:
    """Is there a container runtime here, and does it answer?

    The build's own guard, with its refusal read instead of raised — so
    a person who runs the check and a person who runs the build are told
    the same thing in the same words.
    """
    program = resolve_container_program(options=options)
    try:
        require_container_runtime(ContainerRuntime(program), env=env)
    except MCUHomeError as refusal:
        return _refused("runtime", refusal)
    return HostFinding(check="runtime", ok=True, detail=f"{program} answers")


def _container_image(options: BuildOptions) -> HostFinding:
    """Can a build environment be looked for where this machine may take one?

    Which image a build runs is decided by the package set a context
    pins, so there is no image to look for before a device is built. What
    *can* be answered without one is the question a person actually has
    when a build fails here: can this machine reach a repository it is
    allowed to take an environment from at all.
    """
    repositories = tuple(options.container_repositories)
    if not repositories:
        return HostFinding(
            check="image",
            ok=False,
            detail="no container repository is allowed to deliver a build environment",
            hint=(
                "set build.container_repositories to the repository your build "
                "environments are published in, or build outside a container"
            ),
        )
    client = ImageRegistry()
    answered: list[str] = []
    unreachable: list[str] = []
    malformed: list[str] = []
    for repository in repositories:
        # Read before anything is asked, and kept apart from what a
        # registry answers: a value that is not a repository name was
        # never a question about this machine, and a build would refuse
        # on it before reaching a network.
        try:
            reference = parse_reference(
                repository, default_registry=DOCKER_HUB, what="build environment"
            )
        except MCUHomeError as unreadable:
            # The message alone, as everywhere here: the rendered form
            # would put a second fix line inside one finding's detail.
            malformed.append(f"{repository} is not a repository name ({_message(unreadable)})")
            continue
        try:
            tags = client.tags(reference)
        except (MCUHomeError, OSError) as unanswered:
            unreachable.append(f"{repository} could not be asked ({unanswered})")
            continue
        answered.append(f"{repository} publishes {len(tags)} image(s)")
    if malformed:
        return HostFinding(
            check="image",
            ok=False,
            detail="; ".join([*malformed, *answered, *unreachable]),
            hint=(
                f"every entry in {CONTAINER_REPOSITORIES_OPTION} is a container "
                "repository, written [registry/]path — correct the one above, or "
                "remove it"
            ),
        )
    if not answered:
        return HostFinding(
            check="image",
            ok=False,
            detail="; ".join(unreachable),
            hint=(
                "a build takes its build environment from one of these repositories — "
                "check this machine's network access to them, and log in where one is "
                "private"
            ),
        )
    return HostFinding(
        check="image",
        ok=True,
        detail="; ".join([*answered, *unreachable]),
    )


# --------------------------------------------------------------------------
# The subprocess profile
# --------------------------------------------------------------------------


def _env_store(
    options: BuildOptions, env: Mapping[str, str], *, project: Project | None
) -> HostFinding:
    """Where unpacked build environments go, and whether they can go there."""
    try:
        root = buildenvstore.store_root(dict(env), override=options.env_store)
    except MCUHomeError as refusal:
        return _refused("store", refusal)
    except RuntimeError as unresolvable:
        return _unresolvable("store", ENV_STORE_OPTION, options.env_store, unresolvable)
    shown = _shown(root, project)
    writable, obstacle = _writable(root)
    if not writable:
        return HostFinding(
            check="store",
            ok=False,
            detail=f"{shown} cannot be written ({obstacle})",
            hint=(
                "a build outside a container unpacks its build environment here — "
                "make the directory writable, or set build.env_store to somewhere "
                "this account owns"
            ),
        )
    return HostFinding(
        check="store",
        ok=True,
        detail=f"{shown} ({_entry_count(root)} build environment(s) provisioned)",
    )


def _entry_count(root: Path) -> int:
    """How many finished entries the store at *root* holds."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return 0
    return sum(1 for entry in entries if buildenvstore.provisioned(entry) is not None)


def _python(options: BuildOptions, env: Mapping[str, str]) -> HostFinding:
    """The interpreter that creates a build environment's virtual environment.

    Two things decide whether it can: that it runs at all, and that it
    carries ``venv``. The second is worth asking because a system Python
    without it is the ordinary state of a Debian-like machine, and the
    failure it produces mid-provisioning says nothing about which package
    is missing.
    """
    interpreter = options.python or sys.executable
    found = interpreter if Path(interpreter).is_absolute() else _on_path(interpreter, env)
    if found is None:
        return HostFinding(
            check="python",
            ok=False,
            detail=f"{interpreter} is not on this machine's PATH",
            hint=(
                "a build outside a container creates its build environment with this "
                "interpreter — install it, or set build.python to one that is here"
            ),
        )
    facts = _interpreter_facts(found)
    if facts is None:
        return HostFinding(
            check="python",
            ok=False,
            detail=f"{found} did not answer as a Python interpreter",
            hint="set build.python to a Python that runs on this machine",
        )
    major, minor, venv = facts
    if not venv:
        return HostFinding(
            check="python",
            ok=False,
            detail=f"{found} is Python {major}.{minor} and has no venv module",
            hint=(
                "the build environment is installed into a virtual environment this "
                "interpreter creates:\n"
                "    sudo apt install python3-venv      # Debian, Ubuntu\n"
                "or set build.python to a Python that carries it"
            ),
        )
    return HostFinding(check="python", ok=True, detail=f"{found} is Python {major}.{minor}")


def _developer_python(env: Mapping[str, str]) -> HostFinding:
    """The interpreter a **development** build runs the builder with.

    A different question from the one above and asked of a different
    program: a development build unpacks nothing and creates no virtual
    environment, it runs the builder out of the workspace with the
    ``python3`` on the ``PATH`` the build was started from — the same
    lookup :func:`~mcuhome.workbench.subprocessbuild.developer_launcher`
    makes, so a machine without one hears it here instead of when the
    first step tries to start.
    """
    found = _on_path(BUILDER_INTERPRETER, env)
    if found is None:
        return HostFinding(
            check="python",
            ok=False,
            detail=f"there is no {BUILDER_INTERPRETER} on this PATH",
            hint=(
                "a development build runs MCUHome's builder with your own Python, the "
                "way west does — start the build from the shell you develop in, or "
                f"unset {DEV_WORKSPACE_OPTION} to build against the build environment "
                "MCUHome unpacks itself"
            ),
        )
    facts = _interpreter_facts(found)
    if facts is None:
        return HostFinding(
            check="python",
            ok=False,
            detail=f"{found} did not answer as a Python interpreter",
            hint=f"check it with {BUILDER_INTERPRETER} -V, or repair your PATH",
        )
    major, minor, _venv = facts
    # No ``venv`` here on purpose: a development build installs nothing,
    # so an interpreter without it builds perfectly well.
    return HostFinding(check="python", ok=True, detail=f"{found} is Python {major}.{minor}")


def _interpreter_facts(interpreter: str) -> tuple[int, int, bool] | None:
    """*interpreter*'s version and whether it carries ``venv``, or ``None``.

    ``None`` for a program that did not run or did not answer as a Python
    — the same thing to a caller, and neither is this check's to explain.
    """
    fields = _run_program([interpreter, "-c", _PYTHON_FACTS]).split()
    expected_fields = 3
    if len(fields) != expected_fields or not all(field.isdigit() for field in fields):
        return None
    major, minor, venv = (int(field) for field in fields)
    return major, minor, bool(venv)


def _dev_workspace(workspace: Path, *, project: Project | None) -> HostFinding:
    """The west workspace a development build compiles instead of a package."""
    shown = _shown(workspace, project)
    try:
        checkout = devworkspace.manifest_checkout(workspace)
    except MCUHomeError as refusal:
        return _refused("workspace", refusal)
    return HostFinding(
        check="workspace",
        ok=True,
        detail=f"{shown} is a west workspace; the SDK is {_shown(checkout, project)}",
    )


# --------------------------------------------------------------------------
# What both profiles need
# --------------------------------------------------------------------------


def _signing_imgtool(env: Mapping[str, str], stated: str | None) -> HostFinding:
    """The program that signs what a build produced.

    A stated one is a path like any other and is expanded against the
    environment this check was handed, so a ``~`` without a home
    directory is a refusal and a ``~somebody`` this machine has no
    account for is an error out of the path library — which here are both
    findings like every other, because this call raises nothing.
    """
    try:
        program = find_imgtool(env=dict(env), stated=stated)
    except MCUHomeError as refusal:
        return _refused("imgtool", refusal)
    except RuntimeError as unresolvable:
        return _unresolvable("imgtool", IMGTOOL_OPTION, stated, unresolvable)
    if program is None:
        return HostFinding(
            check="imgtool",
            ok=False,
            detail="imgtool is not available here",
            hint=(
                "imgtool signs the firmware a build produced and is installed with "
                "MCUHome:\n"
                "    pip install imgtool\n"
                "or set signing.imgtool to the one you have"
            ),
        )
    return HostFinding(check="imgtool", ok=True, detail=" ".join(program))


def _cache_root(
    options: BuildOptions, env: Mapping[str, str], *, project: Project | None
) -> HostFinding:
    """Where the compiler cache lives — and that a build without one is fine."""
    root = resolve_cache_root(options=options, env=dict(env))
    if root is None:
        return HostFinding(
            check="cache",
            ok=True,
            detail="no compiler cache on this host; every build compiles from scratch",
            hint="set build.cache_root to give this machine one",
        )
    writable, obstacle = _writable(root)
    shown = _shown(root, project)
    if not writable:
        return HostFinding(
            check="cache",
            ok=False,
            detail=f"{shown} cannot be written ({obstacle})",
            hint=(
                "a build writes its compiler cache here — make the directory "
                "writable, or set build.cache_root to somewhere this account owns"
            ),
        )
    return HostFinding(check="cache", ok=True, detail=str(shown))


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _refused(check: str, refusal: MCUHomeError) -> HostFinding:
    """A refusal, read out as the finding it is.

    The words are the refusal's own: a person who runs this check and a
    person whose build stopped are looking at the same problem, and two
    wordings of it would be two problems to them. The message and the fix
    are taken apart the way the error document takes them apart —
    ``str()`` of a refusal is the *rendered* form, which would put the
    hint into ``detail`` and then again into ``hint``.
    """
    return HostFinding(
        check=check,
        ok=False,
        detail=_message(refusal),
        hint=getattr(refusal, "hint", None) or "",
    )


def _message(refusal: MCUHomeError) -> str:
    """A refusal's own sentence, without the rendering around it."""
    return getattr(refusal, "message", None) or str(refusal)


def _unresolvable(check: str, key: str, value: object, error: Exception) -> HostFinding:
    """A configured path this machine cannot even resolve.

    ``~somebody`` names an account, and a machine that has no such
    account cannot answer what the path means — which comes out of the
    path library as an error rather than as one of this package's
    refusals, and would otherwise leave the check itself raising.
    """
    return HostFinding(
        check=check,
        ok=False,
        detail=f"{key} is {value} and cannot be resolved on this machine ({error})",
        hint=(
            f"`~name` is the home directory of the account *name*, and there is no "
            f"such account here — set {key} to a path this machine has"
        ),
    )


def _writable(path: Path) -> tuple[bool, str]:
    """Whether *path* can be written to, or created where it is not there.

    A directory that does not exist yet is not a problem: a build creates
    the store and the cache. What decides it is the nearest ancestor that
    *does* exist — if nothing may be created in there, nothing below it
    will appear either.
    """
    existing = path
    while not existing.exists():
        parent = existing.parent
        if parent == existing:
            return False, "there is no directory above it that exists"
        existing = parent
    if not existing.is_dir():
        return False, f"{existing} is a file"
    if not os.access(existing, os.W_OK | os.X_OK):
        return False, f"{existing} belongs to somebody else"
    return True, ""


def _shown(path: Path, project: Project | None) -> Path:
    """*path*, relative to *project* where it lies inside it."""
    if project is None:
        return path
    try:
        return path.relative_to(project.root)
    except ValueError:
        return path


def _on_path(program: str, env: Mapping[str, str]) -> str | None:
    """*program* on the PATH this check was handed, or ``None``."""
    return shutil.which(program, path=env.get("PATH"))


def _run_program(argv: Sequence[str]) -> str:
    """What *argv* wrote to stdout, or the empty string if it did not run.

    The seam every probe that starts a program goes through, so a test
    can answer for a host it does not have.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - the argv is this package's own
            list(argv), capture_output=True, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout if completed.returncode == 0 else ""
