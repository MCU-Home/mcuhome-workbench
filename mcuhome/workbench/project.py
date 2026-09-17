# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The project directory.

A user's work lives in a **project directory**: the folder that carries
the **project marker** ``.mcuhome-project-root``. The marker is a
dedicated dotfile that is not the user's to edit — deliberately *not* a
configuration file. A ``mcuhome.yaml`` can plausibly lie around in
folders that are no project root (a copied example, a config snippet),
and only a file that exists for exactly one purpose can never mark one
by accident. Marker = identity; ``mcuhome.yaml`` = project-level
*configuration*, and therefore optional
(:mod:`mcuhome.workbench.configuration`).

What the marker says about itself — the project's layout **version** and
its **id** — belongs to :mod:`mcuhome.workbench.projectfile`, and
resolution here enforces it: a project whose version these tools do not
speak is refused with the upgrade command rather than worked on, and a
project whose file is renamed because an upgrade is running (or died) is
refused with what actually happened
(:mod:`mcuhome.workbench.projectupgrade`).

The layout inside a project::

    .mcuhome-project-root     # the marker: project version and id
    mcuhome.yaml              # project configuration (optional)
    devices/<name>/main.yaml  # one folder per device
    secrets/                  # ALL secrets, no exceptions (mode 700)
      main.yaml               #   project-wide secrets (`!secret`)
      device/<name>.yaml      #   per-device secrets
      builder/<name>.yaml     #   per-builder credentials
      signing/key.yaml        #   the signing key, beside key.pem
    build/                    # build output (disposable)
    .gitignore                # keeps secrets/ and build/ out of git

**Resolution** starts at a stated working directory and searches
**upward** (git-like) for the marker. Two bootstrap exceptions run
*before* any configuration layer is read, because they decide where the
project layer even is: an explicit ``--project-dir`` argument and, as
its fallback, the ``MCUHOME_PROJECT_DIR`` environment variable. Both
disable the search, and both are an error when the named directory
carries no marker — a directory that never asked to be a project must
never be treated as one. They stand outside the five-layer configuration
merge and can never themselves be set from a configuration file.

**Why the working directory and the environment are arguments.** This
library is one process serving several sessions, each with
its own environment (:mod:`mcuhome.model.userpaths`): "where the caller
stands" is the caller's to state, and a server handling two requests
from two projects stands in neither.

This module also owns the two duties that come with the layout:
``mcuhome project init`` (:func:`create_project` — the durable part of the layout,
created once, refusing a non-empty directory) and the secrets hygiene
(:func:`require_secret_file` — ``secrets/`` is created mode
700 and its files 600; every reader checks, insecure permissions draw a
warning, and for key material the tools refuse).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcuhome.model.context import PATCHES_DIR
from mcuhome.model.errors import ConfigError, Location
from mcuhome.model.userpaths import expand

from mcuhome.workbench.diagnostics import Diagnostic
from mcuhome.workbench.projectfile import (
    PROJECT_MARKER_FILE,
    PROJECT_VERSION,
    UPGRADE_MARKER_FILE,
    ProjectFile,
    new_project_id,
    read_project_file,
    require_current,
    write_project_file,
)
from mcuhome.workbench.projectupgrade import BUILD_DIR, in_flight_error

if TYPE_CHECKING:  # pragma: no cover - import cycle, typing only
    from mcuhome.workbench.configuration import Option

__all__ = [
    "BUILDER_SECRETS_DIR",
    "BUILD_DIR",
    "DEVICES_DIR",
    "DEVICE_FILE",
    "DEVICE_SECRETS_DIR",
    "GITIGNORE_LINES",
    "PROJECT_CONFIG_FILE",
    "PROJECT_MARKER_FILE",
    "PROJECT_VERSION",
    "SECRETS_DIR",
    "SIGNING_SECRETS_DIR",
    "NewProject",
    "Project",
    "create_project",
    "ensure_secrets_dir",
    "find_project_root",
    "is_project_root",
    "is_upgrading",
    "read_project",
    "refuse_unknown_device",
    "require_secret_file",
    "resolve_device",
    "resolve_project",
]

#: Project-level configuration, in the project directory. Optional: a
#: project without one simply has an empty project layer.
PROJECT_CONFIG_FILE = "mcuhome.yaml"

#: The home of every device folder.
DEVICES_DIR = "devices"
#: Entry point inside a device folder.
DEVICE_FILE = "main.yaml"
#: ALL secrets live under this directory, no exceptions.
SECRETS_DIR = "secrets"
#: Project-wide secrets inside ``secrets/`` — what ``!secret`` reads.
MAIN_SECRETS_FILE = "main.yaml"
#: One kind of secret per directory below ``secrets/``, named after the
#: kind and in the singular: the directory says what sort of thing is
#: in it, not how many there are. A builder's credentials live here,
#: under the builder's name.
BUILDER_SECRETS_DIR = "builder"
#: The kind directory of a device's own secrets, by device name.
DEVICE_SECRETS_DIR = "device"
#: The kind directory of the project's signing key: the key material,
#: its public half and the YAML that references the two.
SIGNING_SECRETS_DIR = "signing"
#: The YAML inside ``secrets/signing/`` that references the key file.
#: Named after what it is about — one project, one signing key — rather
#: than after the bootloader that verifies against it.
SIGNING_SECRETS_FILE = "key.yaml"

#: What ``mcuhome project init`` keeps out of git. ``secrets/`` is the point of
#: the file; ``build/`` is disposable output that would
#: otherwise be the first accidental commit of every new project.
GITIGNORE_LINES = ("secrets/", "build/")


@dataclass(frozen=True)
class Project:
    """A resolved project directory."""

    root: Path
    #: True when the root carries the marker; False for the
    #: "bare YAML file, no project around it" fallback of
    #: :func:`resolve_device`.
    discovered: bool
    #: What the project file says — its version and its id. None exactly
    #: for the stand-in root above, which has no project file to read.
    file: ProjectFile | None = None

    @property
    def marker(self) -> Path:
        return self.root / PROJECT_MARKER_FILE

    @property
    def id(self) -> str | None:
        """The project's unique id, or None for a stand-in root."""
        return None if self.file is None else self.file.id

    @property
    def config_file(self) -> Path:
        """``mcuhome.yaml`` — the project configuration layer (optional)."""
        return self.root / PROJECT_CONFIG_FILE

    @property
    def devices_dir(self) -> Path:
        return self.root / DEVICES_DIR

    @property
    def secrets_dir(self) -> Path:
        return self.root / SECRETS_DIR

    @property
    def secrets_file(self) -> Path:
        """``secrets/main.yaml`` — the project-wide secrets ``!secret`` reads."""
        return self.secrets_dir / MAIN_SECRETS_FILE

    @property
    def signing_secrets_dir(self) -> Path:
        """``secrets/signing/`` — the key, its public half, the reference."""
        return self.secrets_dir / SIGNING_SECRETS_DIR

    @property
    def signing_secrets_file(self) -> Path:
        """``secrets/signing/key.yaml`` — what references the key file."""
        return self.signing_secrets_dir / SIGNING_SECRETS_FILE

    @property
    def builder_secrets_dir(self) -> Path:
        """Where one builder's credentials file lives, by builder name."""
        return self.secrets_dir / BUILDER_SECRETS_DIR

    @property
    def device_secrets_dir(self) -> Path:
        """Where one device's secrets file lives, by device name."""
        return self.secrets_dir / DEVICE_SECRETS_DIR

    def builder_secrets_file(self, name: str) -> Path:
        """``secrets/builder/<name>.yaml`` — one builder's credentials."""
        return self.builder_secrets_dir / f"{name}.yaml"

    def device_secrets_file(self, name: str) -> Path:
        """``secrets/device/<name>.yaml`` — one device's own secrets."""
        return self.device_secrets_dir / f"{name}.yaml"

    def device_file(self, name: str) -> Path:
        return self.devices_dir / name / DEVICE_FILE

    def device_build_dir(self, name: str) -> Path:
        """``build/<device>/`` — where a build of one device works.

        The convention, in one place: a build of a device that was not
        given a directory of its own works here, which is what makes two
        devices of one project buildable at the same time without either
        of them saying so. A path, not a promise — a device that has
        never been built has none.
        """
        return self.root / BUILD_DIR / name

    def device_patches_dir(self, name: str) -> Path:
        """``devices/<device>/patches/`` — the source patches of one device.

        Laid out as ``<layer>/NNNN-name.patch`` — the build context
        format's own layout, so the folder is carried into a context as
        it stands. It lives beside the device file because that is what
        it belongs to: a patch is part of what this device's firmware
        *is*, and a build picks the folder up because it is there rather
        than because somebody remembered a flag.

        A path, not a promise: the folder is optional and most devices
        have none.
        """
        return self.devices_dir / name / PATCHES_DIR

    def device_names(self) -> list[str]:
        if not self.devices_dir.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.devices_dir.iterdir()
            if entry.is_dir() and (entry / DEVICE_FILE).is_file()
        )

    def to_dict(self) -> dict[str, Any]:
        """The project as a document, for the results that carry one.

        Four facts and no derived paths: where it is, which project it
        is, whether a marker was actually found, and which layout
        version its file states. Everything else a client could want is
        computed from *root* by the same rules this class uses, and a
        document that listed them would go stale the day one of them
        moves.
        """
        return {
            "root": str(self.root),
            "id": self.id,
            "discovered": self.discovered,
            "version": None if self.file is None else self.file.version,
        }


def is_project_root(path: Path) -> bool:
    """Whether *path* carries the project marker."""
    return (path / PROJECT_MARKER_FILE).is_file()


def is_upgrading(path: Path) -> bool:
    """Whether *path* is a project whose file an upgrade has renamed."""
    return (path / UPGRADE_MARKER_FILE).is_file()


def find_project_root(start: Path, *, allow_upgrading: bool = False) -> Path | None:
    """Walk *start* upwards and return the first directory carrying the marker.

    A directory that is *being upgraded* stops the walk too, with the
    refusal that says so: its marker is renamed for the duration, and
    walking past it would end in "no project found here" for a project
    that is plainly there.

    *allow_upgrading* answers that directory instead of refusing, for
    the caller that exists to describe such a project rather than work
    on it (:func:`resolve_project` says which one).
    """
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if is_project_root(candidate):
            return candidate
        if is_upgrading(candidate):
            if allow_upgrading:
                return candidate
            raise in_flight_error(candidate)
    return None


def read_project(
    root: Path, *, require_version: bool = True, allow_upgrading: bool = False
) -> Project:
    """The project in *root*, its file read and — by default — checked.

    *require_version* is False for exactly one caller: the upgrade
    itself, which exists to make an outdated project current again.

    *allow_upgrading* reads the renamed marker of a project an upgrade
    is holding — or died holding — instead of failing over the marker
    that is not there. The project it answers is the one on disk right
    now, at whatever layout version the upgrade had reached.
    """
    marker = root / PROJECT_MARKER_FILE
    if allow_upgrading and not marker.is_file() and is_upgrading(root):
        marker = root / UPGRADE_MARKER_FILE
    file = read_project_file(marker, root=root)
    if require_version:
        require_current(file)
    return Project(root=root, discovered=True, file=file)


def _project_dir_option() -> Option:
    """The declaration of ``project.dir``, for its two spellings.

    Imported here rather than at module level: the configuration layer
    reads this module, so the dependency only runs the other way once,
    inside the bootstrap that needs the flag and the variable the
    registry derives. Nothing else in this module knows either spelling
    — the declaration is the single source of both.
    """
    from mcuhome.workbench.configuration import option

    return option("project.dir")


def _refuse_no_marker(directory: Path, *, named_by: str) -> ConfigError:
    return ConfigError(
        f'"{directory}" is not an MCUHome project directory: it has no {PROJECT_MARKER_FILE}.',
        hint=(
            f"{named_by} must name the directory that carries the {PROJECT_MARKER_FILE} "
            "marker. Check the path, or create a project there first with:\n"
            "    mcuhome project init"
        ),
    )


def resolve_project(
    project_dir: Path | str | None = None,
    *,
    env: Mapping[str, str],
    cwd: Path,
    require_version: bool = True,
    allow_upgrading: bool = False,
) -> Project:
    """Resolve the project directory: the bootstrap ladder.

    ``--project-dir`` (*project_dir*) first, ``MCUHOME_PROJECT_DIR`` as its
    fallback; either disables the search and is an error when the named
    directory carries no marker. With neither set, the search walks
    *cwd* upward and takes the first directory carrying the marker.

    Whichever way the directory was found, its project file is read and
    its version checked — a project MCUHome does not speak is refused
    here, once, rather than in each of the commands. *require_version*
    turns only that last step off, for the upgrade that fixes it.

    *allow_upgrading* is the other refusal turned off: a project an
    upgrade is holding, or died holding, is resolved and described
    instead of refused. It exists for the caller a person runs *because*
    something refused them — a command that says what the state of the
    project is — and never for one that works on the project: what a
    half-migrated layout holds is exactly what nothing may act on.

    Both *env* and *cwd* are stated, never read from the process — the
    module docstring says why.
    """
    declared = _project_dir_option()
    for value, named_by in (
        (project_dir, declared.flag),
        (env.get(declared.env_var), declared.env_var),
    ):
        if not value:
            continue
        directory = expand(value, env)
        if not directory.is_absolute():
            directory = (cwd / directory).resolve()
        if not directory.is_dir():
            raise ConfigError(
                f'The project directory "{value}" ({named_by}) does not exist.',
                hint=(
                    "check the path, or create the project first with:\n    mcuhome project init"
                ),
            )
        if not is_project_root(directory):
            if not is_upgrading(directory):
                raise _refuse_no_marker(directory, named_by=named_by)
            if not allow_upgrading:
                raise in_flight_error(directory)
        return read_project(
            directory.resolve(),
            require_version=require_version,
            allow_upgrading=allow_upgrading,
        )

    found = find_project_root(cwd, allow_upgrading=allow_upgrading)
    if found is None:
        raise ConfigError(
            "No MCUHome project found here.",
            hint=(
                f"a project directory carries a {PROJECT_MARKER_FILE} marker, and none was "
                f"found from {cwd.resolve()} upward. Run this inside a project, pass "
                "--project-dir /path/to/project, or create one here with:\n"
                "    mcuhome project init"
            ),
        )
    return read_project(found, require_version=require_version, allow_upgrading=allow_upgrading)


def refuse_unknown_device(project: Project, name: str) -> ConfigError:
    """The refusal for a device name the project does not have.

    One wording for every call that takes a device by name, because they
    all fail the same way and a user who mistyped wants the same thing
    from each of them: the devices that *are* there, and where "there"
    is. The project root is the location rather than a file, since the
    file the name would have named is exactly what does not exist.
    """
    known = project.device_names()
    listing = ", ".join(known) if known else "none yet"
    return ConfigError(
        f'There is no device called "{name}" in this project.',
        location=Location(file=project.root),
        hint=f"devices in {project.root}: {listing}",
    )


def _looks_like_path(spec: str) -> bool:
    return "/" in spec or "\\" in spec or spec.endswith((".yaml", ".yml"))


def _entry_for_path(path: Path, spec: str) -> Path:
    if path.is_dir():
        entry = path / DEVICE_FILE
        if not entry.is_file():
            raise ConfigError(
                f'The device folder "{spec}" has no {DEVICE_FILE}.',
                hint=(
                    f"every device is a folder with a {DEVICE_FILE} entry point; "
                    f"create {spec}/{DEVICE_FILE}"
                ),
            )
        return entry
    if path.is_file():
        return path
    raise ConfigError(
        f'No configuration found at "{spec}".',
        hint="check the path, or pass a device name that exists under devices/",
    )


def resolve_device(
    spec: str,
    *,
    env: Mapping[str, str],
    cwd: Path,
    project_dir: Path | str | None = None,
) -> tuple[Project, Path]:
    """Resolve a ``<device>`` argument to its project and entry file.

    Two forms, tried in this order:

    1. a **device name** — ``<project>/devices/<name>/main.yaml``, with
       the project resolved through :func:`resolve_project`'s ladder;
    2. an **explicit path** — a device folder (``<path>/main.yaml``) or
       a bare YAML file (``<path>`` itself).

    Form 2 is what makes ``mcuhome device validate path/to/example.yaml``
    work outside any project: when no marker is found above an
    explicitly given file, the file's own directory becomes the project
    root (``discovered=False``), so ``!secret`` still resolves against a
    ``secrets/main.yaml`` sitting next to the configuration.

    *cwd* is required for the reason :func:`resolve_project` gives, and
    doubly so here: a relative ``<device>`` path means nothing without
    the directory it is relative to.
    """
    cwd = cwd.resolve()
    candidate_path = (cwd / spec).resolve()

    # 1. Device name against the project, unless the argument is
    #    obviously a path. An explicit project directory makes this the
    #    only accepted form for plain names.
    if not _looks_like_path(spec):
        project = resolve_project(project_dir, env=env, cwd=cwd)
        entry = project.device_file(spec)
        if entry.is_file():
            return project, entry
        if project_dir is not None or not candidate_path.exists():
            raise refuse_unknown_device(project, spec)

    # 2. Explicit path: a device folder or a bare YAML file.
    entry = _entry_for_path(candidate_path, spec)
    root = find_project_root(entry.parent)
    if root is None:
        # A configuration file outside any project — its own directory
        # stands in for one, which is what makes secrets/main.yaml next
        # to the file work.
        return Project(root=entry.parent, discovered=False), entry
    return read_project(root), entry


# --------------------------------------------------------------------------
# Secrets hygiene
# --------------------------------------------------------------------------

#: Permission bits a secrets file must not carry: anything that lets the
#: group or the world read, write or execute it.
_EXPOSED_BITS = 0o077


def require_secret_file(
    path: Path,
    *,
    key_material: bool,
    on_warning: Callable[[Diagnostic], None] | None = None,
) -> None:
    """The permission guard every reader of a secrets file runs.

    ``secrets/`` files are created mode 600, and this is the other half
    of that promise: a file that group or world can reach draws a
    warning through *on_warning* — an ``exposed_secret_file``
    :class:`~mcuhome.workbench.diagnostics.Diagnostic` naming the file,
    so a client can show it where the file is — and when it holds
    **key material** —
    signing keys, future Matter/attestation keys — the read is refused
    outright, because a warning about a leaked private key is a
    notification, not a protection.

    On platforms whose ``stat`` does not carry POSIX permission bits the
    guard is a no-op rather than a guess. A missing file is the caller's
    case to handle — this refuses over what is there, it does not require
    anything to be.
    """
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    exposed = mode & _EXPOSED_BITS
    if not exposed:
        return
    if key_material:
        raise ConfigError(
            f"MCUHome refuses to use the key material in {path}: "
            f"the file is accessible to other users (mode {mode:03o}).",
            hint=(
                f"a private key that others can read is compromised the moment it "
                f"exists. Restrict it first:\n"
                f"    chmod 600 {path}\n"
                f"and keep the secrets directory itself at mode 700."
            ),
        )
    if on_warning is not None:
        on_warning(
            Diagnostic.warning(
                f"{path} is readable by other users (mode {mode:03o}, expected 600).",
                kind="exposed_secret_file",
                location=Location(file=path),
                hint=f"restrict it to its owner:\n    chmod 600 {path}",
            )
        )


def _mkdir_private(directory: Path) -> None:
    """Create *directory* with mode 700, tightening an existing one."""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)


def ensure_secrets_dir(project_root: Path, *parts: str) -> Path:
    """``secrets/[parts…]`` under *project_root*, created mode 700 throughout.

    Every writer of a secrets file goes through this rather than
    ``mkdir(parents=True)``: parent directories created as a side effect
    inherit the umask, and an 0755 ``secrets/`` created on the way to a
    600 file would undo the layout's promise while looking correct.
    """
    directory = project_root / SECRETS_DIR
    _mkdir_private(directory)
    for part in parts:
        directory = directory / part
        _mkdir_private(directory)
    return directory


# --------------------------------------------------------------------------
# mcuhome project init
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NewProject:
    """What ``mcuhome project init`` created."""

    project: Project
    #: Every path this call created or changed, in creation order — what
    #: the command reports, so a user sees exactly what appeared.
    created: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready, every declared key present.

        The paths are absolute, as they were written: what this answers
        is where things are on the machine that ran the call, and a
        client that shows them relative to something knows what to make
        them relative to.
        """
        return {
            "project": self.project.to_dict(),
            "created": [str(path) for path in self.created],
        }


def create_project(root: Path, *, force: bool = False) -> NewProject:
    """Create the durable part of a project in *root*.

    The marker, ``mcuhome.yaml``, ``devices/``, ``secrets/`` (mode 700),
    the trust anchors of the package registries this workbench ships one
    for, and a ``.gitignore`` keeping ``secrets/`` and ``build/`` out of
    git.
    A non-empty directory draws a refusal that lists what is there;
    *force* proceeds anyway. Even under *force* an existing
    ``mcuhome.yaml`` is left alone — it is the user's configuration —
    while the marker and the ``.gitignore`` are completed to what the
    layout requires (missing ignore lines are appended, never rewritten
    over a user's file).

    The marker is written with the current project version and a project
    id drawn once (:mod:`mcuhome.workbench.projectfile`). An **existing**
    marker is left exactly as it is, whatever version it states: making
    an old project current is the upgrade's job, and init must not do it
    silently on a directory a user pointed ``--force`` at.

    **The trust anchors are written here and nowhere else.** A project's
    trust roots are part of what a project *is*, decided once by whoever
    creates it and visible in ``secrets/trust-anchor/`` from that moment
    on. A build that found one missing and wrote it would be deciding
    what to trust on the user's behalf, while about to download
    something; it refuses instead and points back here. An anchor file
    that already exists is left alone, on a first run and on a
    ``--force`` re-run alike.
    """
    root = root.resolve()
    if root.exists() and not root.is_dir():
        raise ConfigError(
            f'"{root}" is not a directory.',
            hint="mcuhome project init creates a project in a directory; point it at one",
        )
    if root.is_dir() and is_upgrading(root):
        raise in_flight_error(root)
    if root.is_dir() and not force:
        entries = sorted(entry.name for entry in root.iterdir())
        if entries:
            listing = ", ".join(entries[:8]) + (", …" if len(entries) > 8 else "")
            raise ConfigError(
                f'The directory "{root}" is not empty ({listing}).',
                hint=(
                    "mcuhome project init expects an empty directory so it cannot damage "
                    "existing work. Re-run with --force to create the project here "
                    "anyway (existing files may be overwritten)."
                ),
            )

    created: list[Path] = []
    root.mkdir(parents=True, exist_ok=True)

    marker = root / PROJECT_MARKER_FILE
    if not marker.is_file():
        write_project_file(
            marker,
            ProjectFile(root=root, version=PROJECT_VERSION, id=new_project_id()),
        )
        created.append(marker)

    config = root / PROJECT_CONFIG_FILE
    if not config.is_file():
        config.write_text(
            "# MCUHome project configuration.\n"
            "# Options set here apply to this project and win over your user\n"
            "# and system configuration; the command line and MCUHOME_*\n"
            "# variables win over this file.\n",
            encoding="utf-8",
        )
        created.append(config)

    devices = root / DEVICES_DIR
    if not devices.is_dir():
        devices.mkdir()
        created.append(devices)

    secrets = root / SECRETS_DIR
    existed = secrets.is_dir()
    _mkdir_private(secrets)
    if not existed:
        created.append(secrets)

    # Imported here rather than at the top: the registry client knows
    # about the project layout, so the arrow points that way, and one at
    # module level pointing back would be a cycle.
    from mcuhome.workbench.packageregistry import install_trust_anchors

    created.extend(install_trust_anchors(root))

    gitignore = root / ".gitignore"
    if gitignore.is_file():
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        missing = [line for line in GITIGNORE_LINES if line not in lines]
        if missing:
            text = gitignore.read_text(encoding="utf-8")
            if text and not text.endswith("\n"):
                text += "\n"
            gitignore.write_text(text + "\n".join(missing) + "\n", encoding="utf-8")
            created.append(gitignore)
    else:
        gitignore.write_text("\n".join(GITIGNORE_LINES) + "\n", encoding="utf-8")
        created.append(gitignore)

    return NewProject(
        project=Project(root=root, discovered=True, file=read_project_file(marker, root=root)),
        created=tuple(created),
    )
