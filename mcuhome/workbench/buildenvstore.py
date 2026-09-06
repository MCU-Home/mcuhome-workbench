# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Build environment packages, turned into a runnable environment on this host.

The subprocess profile of the build environment specification runs the
builder as an ordinary process, which means the environment has to exist
as files on this machine rather than as a container image. This module is
the step that puts it there: the packages a build pins are acquired
verified (:func:`~mcuhome.workbench.orchestrator.acquire_package`),
unpacked into a per-user store, finalized once, and frozen read-only.

**Where it goes.** ``${XDG_CACHE_HOME:-~/.cache}/mcuhome/build-environments/``,
one entry per package: ``<package name>-<version>``. Always under the
user's home and never a system path — an environment is derivable,
disposable data, not something installed. The location is a parameter
here; making it configurable is not this module's business.

**Why an entry is either complete or absent.** A build environment is
1.5 GB of tree that takes minutes to unpack and finalize, and several
builds may want the same one at the same moment. Unpacking therefore
happens in a staging directory and the entry comes into existence by a
single :func:`os.rename` — the one operation the filesystem performs
atomically — so nothing ever observes a half-unpacked tree. What makes
that directory an *environment* is the completion marker written as the
very last file in it: :func:`provisioned` is the only way into the
store and answers ``None`` without one, so an interrupted run leaves
nothing any build can find, and the next attempt throws the remains away
and starts again. A lock around the whole sequence makes a second
provisioner of the same package wait and then find the finished entry
rather than build a second one over it.

**Why it is frozen.** The entry is shared by every build that pins those
packages, including builds running concurrently, so nothing may write
into it — the specification's own rule ("treat it as read-only in both
profiles"). Freezing is a plain ``chmod``: files lose their write bit,
directories lose theirs, and a build that tries to patch a tree in place
fails loudly instead of corrupting the environment of a build running
next to it. Patched trees are copies made elsewhere; that is the
builder's side of the same rule.

Clearing the store is therefore a two-step affair, and the README says
so: ``chmod -R u+w`` first, then ``rm -rf``.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mcuhome.model.errors import BuildError
from mcuhome.model.userpaths import expand, home

from mcuhome.workbench.orchestrator import SDK_MAX_BYTES, acquire_package
from mcuhome.workbench.packageregistry import RegistrySource
from mcuhome.workbench.resolve_pins import SDK_SOURCE

try:  # pragma: no cover - the import itself is the platform check
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "DEFAULT_BOUND",
    "EXTRACTION_BOUNDS",
    "GIT_CONFIG_FILE",
    "MARKER_FILE",
    "SDK_KIND",
    "STORE_DIR",
    "TOOLS_KIND",
    "TOOLS_MANIFEST",
    "VENV_DIR",
    "WHEELS_DIR",
    "WORKSPACE_KIND",
    "WORKSPACE_MANIFEST",
    "BuildEnvironmentError",
    "PythonRequirement",
    "StoreEntry",
    "entry_directory",
    "extraction_bound",
    "git_config_file",
    "provision",
    "provisioned",
    "require_manifest",
    "required_python",
    "store_root",
]

# --------------------------------------------------------------------------
# The store's vocabulary
# --------------------------------------------------------------------------

#: The store, under MCUHome's directory in the user's cache home.
STORE_DIR = "build-environments"

#: The sources a build environment is assembled from, by the name they
#: are published under. They are the registry's source names, so the same
#: strings select a source, a bound and a finalization.
SDK_KIND = SDK_SOURCE
WORKSPACE_KIND = "build-workspace"
TOOLS_KIND = "build-tools"

#: What each package says about itself, at the top of its own tree. The
#: entry point checks for exactly these two files before it does
#: anything, and provisioning checks for them for the same reason: a tree
#: that does not carry its own statement is not the package it was
#: acquired as.
WORKSPACE_MANIFEST = "build-workspace.json"
TOOLS_MANIFEST = "build-tools.json"

#: Inside the tools package: the wheel set a build's virtual environment
#: is created from, and where that environment is created. The package
#: ships wheels and not a ready environment because a virtual environment
#: is not relocatable — absolute interpreter paths and shebangs — so it
#: has to be created at its final location, which is here.
WHEELS_DIR = "wheels"
VENV_DIR = "venv"

#: Written as the last file inside an entry: which package this tree is.
#: A JSON object, so a later field can be added without a second file.
MARKER_FILE = ".mcuhome-provisioned"

#: The git configuration a build of this environment is pointed at, in
#: the workspace entry. See :func:`git_config_file`.
GIT_CONFIG_FILE = ".mcuhome-gitconfig"

#: Where a provisioning that has not finished yet writes, and where the
#: locks live. Both are dot names inside the store so that a listing of
#: the store shows entries and nothing else.
_STAGING_PREFIX = ".staging-"
_LOCK_DIR = ".locks"

_GIB = 1024**3

#: How much each kind of package may unpack to. Not a tuning knob — a
#: package is trusted before it is unpacked, by its pinned hash — but a
#: corrupt or hostile archive expands without limit, and the cap turns
#: that into a bounded read instead of a machine with no memory and no
#: disk left.
#:
#: The values are an order of magnitude above what MCUHome's own packages
#: actually unpack to (measured at 0.1.10.dev1: the SDK 1.2 MB, the
#: workspace 1.53 GiB, the tools 951 MiB), because a package built by
#: somebody else — a workspace with more modules, a tools package with
#: several toolchains — is a legitimately much larger thing than ours.
EXTRACTION_BOUNDS = {
    SDK_KIND: SDK_MAX_BYTES,
    WORKSPACE_KIND: 20 * _GIB,
    TOOLS_KIND: 10 * _GIB,
}

#: What a kind nobody has bounded gets. The SDK's bound, because it is
#: the smallest and an unknown package is the one to be least generous
#: with.
DEFAULT_BOUND = SDK_MAX_BYTES

#: Wheel tags. ``cp313-cp313`` names one interpreter minor exactly;
#: ``cp311-abi3`` names the oldest one it works on; ``py3-none`` names
#: none at all.
_ABI_EXACT = re.compile(r"cp(\d)(\d+)t?\Z")
_ABI_STABLE = re.compile(r"abi(\d)\Z")
_PY_TAG = re.compile(r"(?:cp|pp)(\d)(\d+)\Z")


class BuildEnvironmentError(BuildError):
    """The store cannot deliver the environment that was asked for."""


@dataclass(frozen=True)
class StoreEntry:
    """One provisioned package in the store."""

    #: The source the package was published under — the key into
    #: :data:`EXTRACTION_BOUNDS` and the finalization.
    kind: str
    #: The concrete package name, architecture suffix and all.
    name: str
    version: str
    sha256: str
    #: The entry directory: the package's own tree, frozen.
    path: Path

    @property
    def marker(self) -> Path:
        return self.path / MARKER_FILE


@dataclass(frozen=True)
class PythonRequirement:
    """Which interpreter a wheel set can be installed by.

    ``exact`` comes from a wheel built against the interpreter's C API
    (``cp313-cp313``): those install into that minor version and no
    other. ``minimum`` comes from a stable-ABI wheel (``cp311-abi3``),
    which installs into that minor and everything after it. Wheels that
    are pure Python say nothing and decide nothing.
    """

    exact: tuple[int, int] | None = None
    minimum: tuple[int, int] | None = None
    #: The wheel the requirement was read off, for the refusal.
    evidence: str = ""

    def satisfied_by(self, version: tuple[int, int]) -> bool:
        if self.exact is not None:
            return version == self.exact
        if self.minimum is not None:
            return version >= self.minimum
        return True

    def described(self) -> str:
        """The requirement in the words a refusal uses."""
        if self.exact is not None:
            return f"{self.exact[0]}.{self.exact[1]}"
        if self.minimum is not None:
            return f"{self.minimum[0]}.{self.minimum[1]} or newer"
        return "any version"


# --------------------------------------------------------------------------
# Where things are
# --------------------------------------------------------------------------


def store_root(env: dict[str, str], *, override: Path | str | None = None) -> Path:
    """The environment store this *env* describes.

    ``$XDG_CACHE_HOME/mcuhome/build-environments``, or the ``~/.cache``
    form when the variable is unset — the cache home and not the
    configuration home, because everything in here is derivable from
    package hashes and may be deleted at any time.

    *override* wins outright and is expanded the same way, so an operator
    can put the store on a volume with room for it. Resolved from *env*
    rather than from the process, like every other per-user path in
    MCUHome: one process serves several sessions, and a function that
    took an environment and then consulted the process anyway would be
    lying about what it answers for.
    """
    if override is not None:
        return expand(override, env)
    cache_home = env.get("XDG_CACHE_HOME")
    base = expand(cache_home, env) if cache_home else home(env) / ".cache"
    return base / "mcuhome" / STORE_DIR


def entry_directory(root: Path, name: str, version: str) -> Path:
    """Where the package *name* *version* lives in the store at *root*."""
    return root / f"{name}-{version}"


def extraction_bound(kind: str) -> int:
    """How much a package of *kind* may unpack to."""
    return EXTRACTION_BOUNDS.get(kind, DEFAULT_BOUND)


def git_config_file(entry: StoreEntry | Path) -> Path:
    """The git configuration to run a build of this entry with.

    Git refuses to read a repository whose directory belongs to another
    user (its ``dubious ownership`` hardening), and both things a build
    reads out of the workspace's repositories go through git: west
    resolves the manifest's ``import:`` out of them, and Zephyr's version
    stamping shells out to ``git describe``. The first fails loudly, the
    second fails **silently** and changes the firmware — so the exemption
    is worth setting even where the store's owner and the build's user
    are the same account, which is the ordinary case for a per-user
    store.

    Written into the workspace entry (the one with repositories in it)
    rather than into the user's ``~/.gitconfig``
    or a system file, for one reason each: a per-user file would outlive
    the store it talks about and accumulate a line per entry ever
    provisioned, and a system file is not something a build may write.
    The file travels with the entry it exempts and disappears when the
    entry is deleted. A caller points git at it with ``GIT_CONFIG_GLOBAL``.
    """
    path = entry.path if isinstance(entry, StoreEntry) else entry
    return path / GIT_CONFIG_FILE


def provisioned(directory: Path) -> StoreEntry | None:
    """What the entry at *directory* says it is, or ``None``.

    ``None`` for an absent entry and for one whose marker is missing or
    unreadable. The marker is the last thing :func:`provision` writes, so
    a directory without a usable one is an unpacking that was interrupted
    or something that was never one at all — either way not an
    environment, and never used as one.
    """
    try:
        document = json.loads((directory / MARKER_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    try:
        return StoreEntry(
            kind=str(document["kind"]),
            name=str(document["package"]),
            version=str(document["version"]),
            sha256=str(document["sha256"]),
            path=directory,
        )
    except KeyError:
        return None


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------


def provision(
    *,
    kind: str,
    name: str,
    version: str,
    sha256: str,
    env: dict[str, str],
    sources: Sequence[Path] = (),
    registry: RegistrySource | None = None,
    store: Path | str | None = None,
    platform: str | None = None,
    max_bytes: int | None = None,
    interpreter: str | Path | None = None,
    on_line: Callable[[str], None] | None = None,
) -> StoreEntry:
    """The package *name* *version*, in the store and ready to run.

    Acquire (operator directories first, the registry second, the hash
    checked on every path), unpack under the bound for *kind* into a
    staging directory, move that onto the entry with one rename,
    finalize, freeze, and write the marker last. A package that is
    already there is answered without touching the network, the disk or
    the lock.

    **Why the finalization happens after the rename and not before it.**
    A virtual environment is not relocatable: its console scripts carry
    the absolute path of the interpreter they were installed for in their
    shebang, so a ``venv`` created under a staging name and then renamed
    would answer ``bad interpreter`` for ``west`` — the package ships
    wheels rather than a ready environment for exactly this reason, and
    creating it anywhere but at its final path would reintroduce the
    problem. The same holds for the git configuration, which names the
    trees it exempts by absolute path. So the entry directory exists for
    the length of the finalization without being an environment yet, and
    the **marker** is what makes it one: :func:`provisioned` — the only
    way into the store — answers ``None`` for anything without it, an
    interrupted run therefore leaves nothing any build can find, and the
    next provisioning discards what it finds unmarked and starts again.

    *name* is the **concrete** package, architecture suffix and all: the
    store path has to be computable before anything is fetched, so a
    family name that only an index can resolve is refused rather than
    quietly stored under a name that means something else on the next
    machine. Callers resolve the family through the index when they pin
    it, which is where the hash comes from anyway.
    """
    root = store_root(env, override=store)
    entry = entry_directory(root, name, version)
    found = provisioned(entry)
    if found is not None:
        _check_identity(found, sha256, kind)
        return found

    root.mkdir(parents=True, exist_ok=True)
    with _entry_lock(root, entry.name):
        # Between the check above and the lock, another process may have
        # provisioned the whole thing. Asking again is the point of the
        # lock: the loser of the race finds the winner's entry.
        found = provisioned(entry)
        if found is not None:
            _check_identity(found, sha256, kind)
            return found

        # Whatever an interrupted attempt left behind — an unmarked entry
        # directory, a staging tree, the spool beside it. None of it is
        # usable, all of it is in the way, and the lock says nobody else
        # is looking at it.
        _discard(entry)
        if entry.exists():
            raise BuildEnvironmentError(
                f"{entry} could not be removed, and an unpacking that did not finish "
                "cannot be built on.",
                hint=f"remove it by hand — chmod -R u+w {entry} && rm -rf {entry}",
            )
        _discard_staging(root, entry.name)
        staging = root / f"{_STAGING_PREFIX}{entry.name}"
        try:
            _say(on_line, f"Unpacking {name} {version} into {entry}")
            acquired = acquire_package(
                kind=kind,
                name=name,
                version=version,
                sha256=sha256,
                sources=sources,
                into=staging,
                registry=registry,
                platform=platform,
                max_bytes=max_bytes if max_bytes is not None else extraction_bound(kind),
                symlinks=True,
            )
            if acquired.name != name:
                raise BuildEnvironmentError(
                    f"{name} {version} is published as a set of packages, one per platform, "
                    f"and this host's is {acquired.name}.",
                    hint=f"provision {acquired.name} instead — a store entry names one "
                    "package and not a family",
                )
            # Before the rename, so that a host whose Python cannot have
            # this package never gets an entry directory at all: the
            # answer is in the staged tree already, and only the venv
            # itself has to wait for the final path.
            if kind == TOOLS_KIND:
                _check_interpreter(staging, name=name, interpreter=interpreter or sys.executable)
            os.rename(staging, entry)
        except BaseException:
            _discard_staging(root, entry.name)
            raise
        try:
            _finalize(
                entry,
                kind=kind,
                name=name,
                interpreter=interpreter or sys.executable,
                on_line=on_line,
            )
            _freeze(entry)
            _write_marker(entry, kind=kind, name=name, version=version, sha256=sha256)
        except BaseException:
            # Only while there is nothing to take away. Once the marker
            # is there another process may have adopted the entry through
            # the fast path, which does not hold this lock, and deleting
            # it under that process would be the one thing this module
            # promises cannot happen.
            if provisioned(entry) is None:
                _discard(entry)
            raise
        # Outside the arm above: the entry is complete from the marker on,
        # and a directory that is merely still writable is not a reason to
        # throw a finished environment away.
        entry.chmod(0o500)
    return StoreEntry(kind=kind, name=name, version=version, sha256=sha256, path=entry)


def _check_identity(found: StoreEntry, sha256: str, kind: str) -> None:
    """The entry that is there is the package that was asked for.

    Name and version are the entry's path, so what can disagree is the
    hash — a package rebuilt under a version it already had — and the
    kind it was finalized as. Answering with the old bytes would build
    something other than what was pinned, so it is a refusal, and the fix
    is to delete the entry.
    """
    if found.kind != kind:
        raise BuildEnvironmentError(
            f"{found.path} was unpacked as a {found.kind} package and is being asked for "
            f"as a {kind} one.",
            hint=f"delete the entry and let MCUHome unpack it again — "
            f"chmod -R u+w {found.path} && rm -rf {found.path}",
        )
    if found.sha256 != sha256:
        raise BuildEnvironmentError(
            f"{found.path} holds a different build of {found.name} {found.version} "
            f"({found.sha256}) than the one this build pins ({sha256}).",
            hint=f"delete the entry and let MCUHome unpack the pinned one — "
            f"chmod -R u+w {found.path} && rm -rf {found.path}",
        )


def _finalize(
    tree: Path,
    *,
    kind: str,
    name: str,
    interpreter: str | Path,
    on_line: Callable[[str], None] | None,
) -> None:
    """Everything an unpacked package needs before it can be run, once.

    The container profile bakes these same steps into its image at build
    time; here they run once per entry, and never again — the entry is
    frozen immediately afterwards. A kind with nothing to finalize — the
    SDK package, which a build gets delivered rather than assembled from
    — is simply unpacked and frozen.
    """
    if kind == TOOLS_KIND:
        require_manifest(tree, TOOLS_MANIFEST, name)
        _create_venv(tree, interpreter=interpreter, name=name, on_line=on_line)
        return
    if kind == WORKSPACE_KIND:
        workspace = _check_west_config(tree, name)
        _write_git_config(tree, workspace)


def require_manifest(tree: Path, manifest: str, name: str) -> dict:
    """The package's own statement of what it is, or a refusal.

    Public because it is the one check a *developer-supplied* tree can
    still be held to (:func:`mcuhome.workbench.subprocessbuild.environment_from_paths`):
    nothing acquired those bytes, so there is no hash and no marker, and
    what the tree says about itself is all there is.
    """
    try:
        document = json.loads((tree / manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BuildEnvironmentError(
            f"{name} does not carry a readable {manifest}, so it is not the build "
            f"environment package it was acquired as ({error}).",
            hint="acquire the package from a source that publishes MCUHome build "
            "environment packages",
        ) from error
    return document if isinstance(document, dict) else {}


# --------------------------------------------------------------------------
# The tools package: the build's virtual environment, offline
# --------------------------------------------------------------------------


def required_python(wheels: Path) -> PythonRequirement:
    """Which interpreter minor version this wheel set can be installed by.

    A wheel built against the interpreter's C API carries that
    interpreter's tag (``cp313-cp313``) and installs into that minor
    version alone — pip on another one does not consider the file at all,
    and with ``--no-index`` there is nothing else to fall back to. So the
    set as a whole installs on exactly one minor version, and asking the
    wheels is the only way to know which: nothing else in the package
    states it.

    A set whose compiled wheels disagree with each other is not
    installable by any interpreter and is refused here rather than
    half-installed later.
    """
    exact: dict[tuple[int, int], str] = {}
    minimum: tuple[int, int] | None = None
    evidence = ""
    for wheel in sorted(wheels.glob("*.whl")):
        fields = wheel.name[: -len(".whl")].split("-")
        if len(fields) < 5:
            continue
        python_tags, abi_tags = fields[-3], fields[-2]
        for abi in abi_tags.split("."):
            if match := _ABI_EXACT.match(abi):
                exact.setdefault((int(match[1]), int(match[2])), wheel.name)
            elif abi == "none":
                # No ABI tag of its own, but a CPython tag in front of it
                # (``cp313-none-any``) says the same thing: this wheel is
                # for one interpreter version. ``py3-none-any`` — the pure
                # wheels — carries no such tag and decides nothing.
                for tag in python_tags.split("."):
                    if found := _PY_TAG.match(tag):
                        exact.setdefault((int(found[1]), int(found[2])), wheel.name)
            elif _ABI_STABLE.match(abi):
                for tag in python_tags.split("."):
                    if found := _PY_TAG.match(tag):
                        floor = (int(found[1]), int(found[2]))
                        if minimum is None or floor > minimum:
                            minimum, evidence = floor, wheel.name
    if len(exact) > 1:
        wheels_named = ", ".join(name for _, name in sorted(exact.items()))
        raise BuildEnvironmentError(
            f"The Python packages in {wheels} are built for different Python versions "
            f"({wheels_named}), so no interpreter can install all of them.",
            hint="the package is broken — report it to whoever published it",
        )
    if exact:
        version, wheel = next(iter(exact.items()))
        return PythonRequirement(exact=version, evidence=wheel)
    return PythonRequirement(minimum=minimum, evidence=evidence)


def _check_interpreter(tree: Path, *, name: str, interpreter: str | Path) -> list[Path]:
    """This host's Python against the package's wheels. Returns the wheels.

    Separate from creating the environment so that it can run on the
    staged tree, before anything of this package is in the store: a host
    that cannot install the wheel set gets a refusal naming the version
    it needs, and no entry.
    """
    wheels = tree / WHEELS_DIR
    files = sorted(wheels.glob("*.whl"))
    if not files:
        raise BuildEnvironmentError(
            f"{name} carries no Python packages in {WHEELS_DIR}/, so the build's "
            "virtual environment cannot be created.",
            hint="the package is incomplete — acquire it again from a source you trust",
        )
    requirement = required_python(wheels)
    found = _interpreter_version(interpreter)
    if not requirement.satisfied_by(found):
        raise BuildEnvironmentError(
            f"{name} needs Python {requirement.described()}, and {interpreter} is "
            f"{found[0]}.{found[1]}.",
            hint=f"run MCUHome on Python {requirement.described()} — that is the version "
            "current Debian stable ships and the one the Python packages inside this "
            "build environment were built for; a wheel built for one Python version "
            "cannot be installed by another",
        )
    return files


def _create_venv(
    tree: Path,
    *,
    interpreter: str | Path,
    name: str,
    on_line: Callable[[str], None] | None,
) -> None:
    """The build's virtual environment, in place and from the bundled wheels.

    ``--no-index`` is the point rather than a precaution: the wheel set
    in the package is complete, so an install that reached an index would
    be putting something into the environment the package does not
    contain — and the machines this profile exists for often have no
    index to reach.

    The interpreter has been checked before this runs
    (:func:`_check_interpreter`, once on the staged tree and once here),
    because a mismatch is not a failure to work around: pip on the wrong
    minor version would find no candidate for the compiled wheels, and
    the "fix" of building them from source needs a compiler, the
    development headers and a network — the three things a packaged
    environment exists to do without.
    """
    wheels = tree / WHEELS_DIR
    files = _check_interpreter(tree, name=name, interpreter=interpreter)
    venv = tree / VENV_DIR
    _say(on_line, f"Creating the build's Python environment in {venv}")
    _run(
        [str(interpreter), "-m", "venv", str(venv)],
        what=f"create the Python environment for {name}",
        on_line=on_line,
    )
    _run(
        [
            str(venv / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            str(wheels),
            *(str(wheel) for wheel in files),
        ],
        what=f"install the Python packages of {name}",
        on_line=on_line,
    )


def _interpreter_version(interpreter: str | Path) -> tuple[int, int]:
    """The ``(major, minor)`` of the interpreter that will create the venv."""
    if str(interpreter) == sys.executable:
        return sys.version_info[0], sys.version_info[1]
    result = subprocess.run(
        [str(interpreter), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
        capture_output=True,
        text=True,
        check=False,
    )
    fields = result.stdout.split()
    if result.returncode != 0 or len(fields) != 2 or not all(f.isdigit() for f in fields):
        raise BuildEnvironmentError(
            f"{interpreter} did not answer as a Python interpreter, so the build "
            "environment cannot be prepared.",
            hint="name a Python interpreter that runs on this machine",
        )
    return int(fields[0]), int(fields[1])


def _run(argv: Sequence[str], *, what: str, on_line: Callable[[str], None] | None) -> None:
    """A provisioning step, or a refusal carrying the last of its output."""
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    except OSError as error:
        raise BuildEnvironmentError(
            f"MCUHome could not {what}: {argv[0]} did not run ({error}).",
            hint="the build environment is prepared with the Python that runs MCUHome — "
            "check that its virtual environment support is installed",
        ) from error
    if result.returncode == 0:
        return
    output = (result.stderr or result.stdout or "").strip().splitlines()
    for line in output[-_OUTPUT_LINES:]:
        _say(on_line, line)
    tail = " ".join(output[-1:]) or f"exit status {result.returncode}"
    raise BuildEnvironmentError(
        f"MCUHome could not {what}: {tail}",
        hint="the package or this host's Python is not what the environment needs — "
        "the last lines of the failing command are above",
    )


#: How much of a failing step's output a refusal carries into the log.
_OUTPUT_LINES = 20


# --------------------------------------------------------------------------
# The workspace package: west's configuration, and git's ownership check
# --------------------------------------------------------------------------


def _check_west_config(tree: Path, name: str) -> Path:
    """The west workspace inside the package, with ``zephyr.base`` set.

    Verified, never written. West's Zephyr extension writes that setting
    into ``.west/config`` the first time something needs it, and an entry
    is frozen and shared — there is nowhere to write it and no moment at
    which writing it would be safe. The package pre-populates it; a
    package that does not is not usable in this profile and says so here,
    before a build discovers it as a permission error deep inside CMake.
    """
    manifest = require_manifest(tree, WORKSPACE_MANIFEST, name)
    stated = str(manifest.get("workspace") or "workspace")
    # The package says where its workspace is, and the answer has to be
    # inside the package: the value ends up in a git configuration and in
    # what a build is handed, so an absolute or climbing path would point
    # both of them somewhere this entry does not own.
    if stated.startswith("/") or ".." in Path(stated).parts:
        raise BuildEnvironmentError(
            f"{name} says its workspace is at {stated!r}, which is not inside the package.",
            hint="the package is broken — report it to whoever published it",
        )
    workspace = tree / stated
    config = workspace / ".west" / "config"
    parser = configparser.ConfigParser()
    try:
        parser.read_string(config.read_text(encoding="utf-8"), str(config))
    except (OSError, configparser.Error) as error:
        raise BuildEnvironmentError(
            f"{name} carries no readable west configuration at "
            f"{config.relative_to(tree)} ({error}).",
            hint="the package is incomplete — acquire it again from a source you trust",
        ) from error
    if not parser.get("zephyr", "base", fallback="").strip():
        raise BuildEnvironmentError(
            f"{name} does not say where Zephyr is: its west configuration has no zephyr.base.",
            hint="the package has to state it — MCUHome never writes into a build "
            "environment, because the same one is shared by every build that uses it",
        )
    return workspace


def _write_git_config(tree: Path, workspace: Path) -> None:
    """The ownership exemption for the workspace's repositories.

    Scoped to the workspace prefix rather than to everything: the trailing
    ``/*`` covers the repositories inside it, nested submodules included,
    and nothing outside the entry.
    """
    (tree / GIT_CONFIG_FILE).write_text(
        "# Written by MCUHome when this build environment was unpacked.\n"
        "#\n"
        "# git refuses to read a repository whose directory belongs to another\n"
        "# user. A build reads this environment's repositories twice — west\n"
        "# resolves the manifest out of them, and Zephyr stamps the firmware\n"
        "# version with `git describe` — so the refusal has to be lifted for\n"
        "# them, and for nothing else. Point git at this file with\n"
        "# GIT_CONFIG_GLOBAL.\n"
        "[safe]\n"
        f"\tdirectory = {workspace}/*\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Freezing, discarding, marking
# --------------------------------------------------------------------------


def _freeze(tree: Path) -> None:
    """Every file and directory below *tree*, read-only.

    Owner-only, and no group or world bits: the store is one user's
    cache. Execute bits survive — the toolchain, ninja, cmake, gn and the
    entry point are in here and a build spawns all of them. Symlinks are
    skipped rather than followed; the thing a link points at is inside
    the tree and gets its own turn.
    """
    for parent, directories, files in os.walk(tree, topdown=False):
        for name in files:
            _freeze_one(Path(parent) / name)
        for name in directories:
            _freeze_one(Path(parent) / name)


def _freeze_one(path: Path) -> None:
    if path.is_symlink():
        return
    mode = path.lstat().st_mode
    path.chmod(0o500 if mode & 0o100 else 0o400)


def _discard(tree: Path) -> None:
    """Remove *tree*, frozen or not.

    A staging directory that failed after :func:`_freeze` has no write
    bit anywhere, and ``rm -rf`` on that fails the same way an operator's
    does — the directory has to be made writable first. Nothing here
    follows a symlink out of the tree: the mode is only ever changed on
    directories the walk itself descended into.
    """
    if tree.is_symlink() or (tree.exists() and not tree.is_dir()):
        tree.unlink()
        return
    if not tree.exists():
        return
    for parent, _directories, _files in os.walk(tree, topdown=True):
        Path(parent).chmod(0o700)
    shutil.rmtree(tree, ignore_errors=True)


def _discard_staging(root: Path, entry: str) -> None:
    """The staging directory of *entry*, and the scratch files beside it.

    Unpacking spools the decompressed tar next to the directory it fills
    and a registry fetch lands the archive there too, so an attempt that
    was killed rather than raised can leave gigabytes in the store under
    names nothing will ever look at again. They are all named after the
    staging directory, so they can all be swept with it.
    """
    for suffix in ("", ".tar", ".download"):
        path = root / f"{_STAGING_PREFIX}{entry}{suffix}"
        if path.is_dir():
            _discard(path)
        elif path.exists():
            path.unlink()


def _write_marker(tree: Path, *, kind: str, name: str, version: str, sha256: str) -> None:
    """The completion record, the last file written into an entry."""
    marker = tree / MARKER_FILE
    marker.write_text(
        json.dumps(
            {
                "kind": kind,
                "package": name,
                "version": version,
                "sha256": sha256,
                "provisioned": datetime.now(UTC).replace(microsecond=0).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    marker.chmod(0o400)


# --------------------------------------------------------------------------
# One provisioning of one package at a time
# --------------------------------------------------------------------------


@contextmanager
def _entry_lock(root: Path, entry: str) -> Iterator[None]:
    """Hold the store's lock for one entry, waiting for whoever has it.

    ``flock`` on a file beside the store, for the reason the build
    directory's lock uses one: the kernel releases it when the holder
    ends, however it ends, so an interrupted provisioning leaves nothing
    to clean up and nothing to second-guess. This lock **waits** rather
    than refusing — two builds wanting the same environment is the normal
    case, and the second one wants the first one's result.

    Platforms without POSIX advisory locks get no lock. Two provisioners
    there each build their own staging directory and one rename wins;
    correctness rests on the rename, not on the lock.
    """
    if fcntl is None:  # pragma: no cover - Windows
        yield
        return
    locks = root / _LOCK_DIR
    locks.mkdir(parents=True, exist_ok=True)
    handle = (locks / f"{entry}.lock").open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def _say(on_line: Callable[[str], None] | None, line: str) -> None:
    if on_line is not None:
        on_line(line)
