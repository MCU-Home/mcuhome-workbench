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
from mcuhome.workbench.buildtarget import MODE_CONTAINER
from mcuhome.workbench.containerbuild import (
    ContainerRuntime,
    require_container_runtime,
    resolve_cache_root,
    resolve_container_program,
)
from mcuhome.workbench.imgtool import find_imgtool
from mcuhome.workbench.ociregistry import ImageRegistry
from mcuhome.workbench.project import Project

__all__ = [
    "HOST_CHECKS",
    "HostCheckResult",
    "HostFinding",
    "check_build_host",
]

#: What a finding's :attr:`HostFinding.check` may be. A fixed value set,
#: published because a client switches on it: a renderer that knows the
#: seven can order them, group them and say more about one than the
#: finding's own words do. Append-only, and each name is the option or
#: the thing that was examined.
HOST_CHECKS = (
    "container_runtime",
    "container_image",
    "env_store",
    "python",
    "dev_workspace",
    "signing_imgtool",
    "cache_root",
)

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


def check_build_host(
    *,
    options: BuildOptions,
    env: Mapping[str, str],
    project: Project | None = None,
    imgtool: str | None = None,
) -> HostCheckResult:
    """Examine what a build with these *options* needs of this machine.

    The container checks run for ``build.mode = container`` and the
    store and interpreter checks for ``subprocess``; the signing tool and
    the compiler cache are examined either way, because a build needs
    them whichever profile runs it.

    *imgtool* is the resolved ``signing.imgtool``, exactly as
    :func:`~mcuhome.workbench.imgtool.plan_signing` takes it: nothing
    under this surface reads a configuration channel of its own, so a
    host whose signing tool is configured has to be told about it or the
    check would report on a program that build never runs.

    *project*, where a caller has one, is what paths inside it are
    reported relative to. No check needs more of it: everything else a
    build reads has been resolved into *options* already.

    It raises nothing. A probe that could not be run is a finding that
    says so.
    """
    findings: list[HostFinding] = []
    if options.mode == MODE_CONTAINER:
        findings.append(_container_runtime(options, env))
        findings.append(_container_image(options))
    elif (workspace := options.dev_workspace) is not None:
        findings.append(_dev_workspace(workspace, project=project))
    else:
        findings.append(_env_store(options, env, project=project))
        findings.append(_python(options, env))
    findings.append(_signing_imgtool(env, imgtool))
    findings.append(_cache_root(options, env, project=project))
    return HostCheckResult(findings=tuple(findings))


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
        return _refused("container_runtime", refusal)
    return HostFinding(check="container_runtime", ok=True, detail=f"{program} answers")


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
            check="container_image",
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
    for repository in repositories:
        try:
            reference = parse_reference(
                repository, default_registry=DOCKER_HUB, what="build environment"
            )
            tags = client.tags(reference)
        except (MCUHomeError, OSError) as unanswered:
            unreachable.append(f"{repository} could not be asked ({unanswered})")
            continue
        answered.append(f"{repository} publishes {len(tags)} image(s)")
    if not answered:
        return HostFinding(
            check="container_image",
            ok=False,
            detail="; ".join(unreachable),
            hint=(
                "a build takes its build environment from one of these repositories — "
                "check this machine's network access to them, and log in where one is "
                "private"
            ),
        )
    return HostFinding(
        check="container_image",
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
        return _refused("env_store", refusal)
    shown = _shown(root, project)
    writable, obstacle = _writable(root)
    if not writable:
        return HostFinding(
            check="env_store",
            ok=False,
            detail=f"{shown} cannot be written ({obstacle})",
            hint=(
                "a build outside a container unpacks its build environment here — "
                "make the directory writable, or set build.env_store to somewhere "
                "this account owns"
            ),
        )
    return HostFinding(
        check="env_store",
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
    answer = _run_program([found, "-c", _PYTHON_FACTS])
    fields = answer.split()
    expected_fields = 3
    if len(fields) != expected_fields or not all(field.isdigit() for field in fields):
        return HostFinding(
            check="python",
            ok=False,
            detail=f"{found} did not answer as a Python interpreter",
            hint="set build.python to a Python that runs on this machine",
        )
    major, minor, venv = (int(field) for field in fields)
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


def _dev_workspace(workspace: Path, *, project: Project | None) -> HostFinding:
    """The west workspace a development build compiles instead of a package."""
    shown = _shown(workspace, project)
    try:
        checkout = devworkspace.manifest_checkout(workspace)
    except MCUHomeError as refusal:
        return _refused("dev_workspace", refusal)
    return HostFinding(
        check="dev_workspace",
        ok=True,
        detail=f"{shown} is a west workspace; the SDK is {_shown(checkout, project)}",
    )


# --------------------------------------------------------------------------
# What both profiles need
# --------------------------------------------------------------------------


def _signing_imgtool(env: Mapping[str, str], stated: str | None) -> HostFinding:
    """The program that signs what a build produced."""
    program = find_imgtool(env=dict(env), stated=stated)
    if program is None:
        return HostFinding(
            check="signing_imgtool",
            ok=False,
            detail="imgtool is not available here",
            hint=(
                "imgtool signs the firmware a build produced and is installed with "
                "MCUHome:\n"
                "    pip install imgtool\n"
                "or set signing.imgtool to the one you have"
            ),
        )
    return HostFinding(check="signing_imgtool", ok=True, detail=" ".join(program))


def _cache_root(
    options: BuildOptions, env: Mapping[str, str], *, project: Project | None
) -> HostFinding:
    """Where the compiler cache lives — and that a build without one is fine."""
    root = resolve_cache_root(options=options, env=dict(env))
    if root is None:
        return HostFinding(
            check="cache_root",
            ok=True,
            detail="no compiler cache on this host; every build compiles from scratch",
            hint="set build.cache_root to give this machine one",
        )
    writable, obstacle = _writable(root)
    shown = _shown(root, project)
    if not writable:
        return HostFinding(
            check="cache_root",
            ok=False,
            detail=f"{shown} cannot be written ({obstacle})",
            hint=(
                "a build writes its compiler cache here — make the directory "
                "writable, or set build.cache_root to somewhere this account owns"
            ),
        )
    return HostFinding(check="cache_root", ok=True, detail=str(shown))


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _refused(check: str, refusal: MCUHomeError) -> HostFinding:
    """A refusal, read out as the finding it is.

    The words are the refusal's own: a person who runs this check and a
    person whose build stopped are looking at the same problem, and two
    wordings of it would be two problems to them.
    """
    return HostFinding(
        check=check, ok=False, detail=str(refusal), hint=getattr(refusal, "hint", None) or ""
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
