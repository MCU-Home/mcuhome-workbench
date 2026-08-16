# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The project file: identity and version of a project (PO 2026-08-16).

``.mcuhome-project-root`` used to be a marker and nothing else — a
dotfile whose presence made a directory a project, carrying one comment
line so a person finding it knew what it was. It still is that marker,
and the rule that identity is *presence* has not changed. What changed
is that it now also answers two questions no other file can:

``version``
    Which layout of a project this is. The tools refuse to work on a
    project whose version they do not speak, and
    :mod:`mcuhome.workbench.projectupgrade` migrates it forward. Today's
    version is :data:`PROJECT_VERSION`; a project created before this
    file had any content is **version 0**, which is what a missing
    ``version`` key means.
``id``
    A UUIDv7 drawn once, when the project is created, and never again.
    It travels with the project (the file is committed), so two clones
    of one project are one project — which is the property everything
    keyed by "this project" needs, from an upgrade confirmation to a
    cache directory that must be the same tomorrow.

**The format is TOML, and the file is not the user's to edit.** Both
halves of that are deliberate: the whole project speaks YAML, so a file
in a different format reads as belonging to a different owner, and the
header says the rest in words. Nothing here is configuration — that is
``mcuhome.yaml``, which is the user's, and the two must not be
confusable.

**The short id.** A UUIDv7 is mostly a timestamp: of
``0198f5c2-9c41-7a3e-8b6d-2f7c14a9e005`` the first two groups are the
draw time, the third begins with the version nibble ``7`` and the fourth
with the variant bits, so the only fully random part is the last group.
:attr:`ProjectFile.short_id` is its last six hex characters — 24 random
bits, short enough to type at a prompt, and the form a person confirms
an upgrade with when a script would paste the whole UUID.
"""

from __future__ import annotations

import secrets
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomli_w
from mcuhome.model.errors import ConfigError

__all__ = [
    "MARKER_FILE",
    "PROJECT_VERSION",
    "SHORT_ID_LENGTH",
    "UPGRADE_FILE",
    "ProjectFile",
    "ProjectFileError",
    "ProjectUpgradeRequired",
    "ProjectVersionUnsupported",
    "UpgradeRecord",
    "new_project_id",
    "read_project_file",
    "require_current",
    "write_project_file",
]

#: The project file. Its presence — not its content — makes a directory
#: a project; the content says which version that project speaks.
MARKER_FILE = ".mcuhome-project-root"

#: What the project file is called while an upgrade is running: the
#: upgrade renames it, so no other command can start work on a project
#: that is being rewritten under it (:mod:`~mcuhome.workbench.projectupgrade`).
UPGRADE_FILE = f"{MARKER_FILE}.upgrade"

#: The project layout this build of MCUHome speaks. Raised by exactly one
#: thing: a migration that produces the new layout.
PROJECT_VERSION = 1

#: How many trailing hex characters of the id make the short form.
SHORT_ID_LENGTH = 6

#: The header every written project file carries. The comment is the
#: first thing anyone opening the file reads, and it is the only place
#: that can say "not yours to edit" to someone who never runs ``--help``.
HEADER = (
    "# This file marks the root of an MCUHome project.\n"
    "#\n"
    "# DO NOT EDIT. MCUHome maintains this file itself, and changing it by\n"
    "# hand can break the whole project. Your own settings belong in\n"
    "# mcuhome.yaml next to it.\n"
    "\n"
)


class ProjectFileError(ConfigError):
    """The project file is unreadable, or says something impossible."""


class ProjectUpgradeRequired(ConfigError):
    """The project speaks an older layout than these tools do."""


class ProjectVersionUnsupported(ConfigError):
    """The project speaks a *newer* layout than these tools do."""


@dataclass(frozen=True)
class UpgradeRecord:
    """The ``[upgrade]`` table an upgrade in flight writes about itself.

    Label, not decision: which process, since when, which migration. The
    lock on the file decides whether that process is still alive; this
    only makes the answer readable (same rule as
    :mod:`mcuhome.workbench.buildlock`).
    """

    started: str = ""
    process: int = 0
    host: str = ""
    running: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "process": self.process,
            "host": self.host,
            "running": self.running,
        }


@dataclass(frozen=True)
class ProjectFile:
    """What one project's file says: where it is, its version, its identity."""

    root: Path
    version: int
    #: The project's UUIDv7 — absent only in a version-0 project, which
    #: predates the id and gets one from the first migration.
    id: str | None = None
    #: Present exactly while an upgrade is running or was interrupted.
    upgrade: UpgradeRecord | None = None

    @property
    def current(self) -> bool:
        return self.version == PROJECT_VERSION

    @property
    def short_id(self) -> str | None:
        """The last six hex characters of the id — see the module docstring."""
        return None if self.id is None else self.id[-SHORT_ID_LENGTH:]

    @property
    def token(self) -> str:
        """What ``--confirm-upgrade`` expects for this project.

        The short id, or — for a version-0 project, which has no id yet
        because the first migration is what draws it — the project
        directory's own name. Either way it is something the user can
        see and type, and something that differs between two projects.
        """
        return self.short_id or self.root.name

    def matches(self, given: str) -> bool:
        """Whether *given* names this project: full id, short id, or token."""
        value = given.strip().lower()
        if not value:
            return False
        candidates = {self.token.lower()}
        if self.id is not None:
            candidates.update({self.id.lower(), self.id.replace("-", "").lower()})
        return value in candidates


def new_project_id() -> str:
    """Draw a project id: a UUIDv7 (RFC 9562), time-ordered and unique.

    Built here rather than taken from the standard library because
    ``uuid.uuid7()`` arrived in Python 3.14 and MCUHome supports 3.11
    upward — the build container itself runs 3.13. **Once 3.14 is the
    floor, delete this function and call ``uuid.uuid7()``**: the result
    is the same string, drawn the same way.

    The layout, most significant bit first: 48 bits of Unix time in
    milliseconds, the 4-bit version ``0111``, 12 random bits, the 2-bit
    variant ``10``, 62 random bits.
    """
    milliseconds = int(time.time() * 1000) & 0xFFFF_FFFF_FFFF
    value = (
        (milliseconds << 80)
        | (0x7 << 76)
        | (secrets.randbits(12) << 64)
        | (0b10 << 62)
        | secrets.randbits(62)
    )
    return str(uuid.UUID(int=value))


def _refuse(path: Path, problem: str) -> ProjectFileError:
    return ProjectFileError(
        f"The project file {path} {problem}.",
        hint=(
            "MCUHome writes this file itself and it is not meant to be edited. "
            "Restore it from your version control or your backup; a project "
            "without a readable project file cannot be used."
        ),
    )


def read_project_file(path: Path, *, root: Path | None = None) -> ProjectFile:
    """Read one project file — the marker, or the ``.upgrade`` form of it.

    A file with no ``version`` key is version 0: that is what every
    project created before the file had content looks like, and the
    first migration is what gives it a version and an id.
    """
    path = Path(path)
    root = Path(root) if root is not None else path.parent
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise _refuse(path, f"cannot be read ({error.strerror or error})") from error
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise _refuse(path, f"is not valid TOML ({error})") from error

    version = data.get("version", 0)
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise _refuse(path, f"states an impossible version ({version!r})")

    identifier = data.get("id")
    if identifier is not None:
        if not isinstance(identifier, str):
            raise _refuse(path, f"states an id that is not text ({identifier!r})")
        try:
            uuid.UUID(identifier)
        except ValueError:
            raise _refuse(path, f'states an id that is not a UUID ("{identifier}")') from None

    upgrade = None
    raw = data.get("upgrade")
    if isinstance(raw, dict):
        process = raw.get("process", 0)
        upgrade = UpgradeRecord(
            started=str(raw.get("started", "")),
            process=process if isinstance(process, int) and not isinstance(process, bool) else 0,
            host=str(raw.get("host", "")),
            running=str(raw.get("running", "")),
        )

    return ProjectFile(root=root, version=version, id=identifier, upgrade=upgrade)


def write_project_file(path: Path, file: ProjectFile) -> None:
    """Write *file* to *path*, header and all.

    Plain write, no atomic replace: the only writer is an upgrade, and
    an upgrade has renamed the file to :data:`UPGRADE_FILE` for the whole
    time it works — no other command can even find the project, so there
    is no half-written state anyone else could read.
    """
    data: dict[str, Any] = {"version": file.version}
    if file.id is not None:
        data["id"] = file.id
    if file.upgrade is not None:
        data["upgrade"] = file.upgrade.to_dict()
    Path(path).write_text(HEADER + tomli_w.dumps(data), encoding="utf-8")


def require_current(file: ProjectFile) -> ProjectFile:
    """Return *file* when these tools speak its version, or refuse in words."""
    if file.current:
        return file
    if file.version > PROJECT_VERSION:
        raise ProjectVersionUnsupported(
            f'The project in "{file.root}" was created by a newer version of MCUHome '
            f"(project version {file.version}; this one speaks {PROJECT_VERSION}).",
            hint=(
                "update MCUHome to a version that knows this project layout — "
                "an older build must not guess at a newer project."
            ),
        )
    raise ProjectUpgradeRequired(
        f'The project in "{file.root}" needs an upgrade '
        f"(project version {file.version}, expected {PROJECT_VERSION}).",
        hint=(
            "back the project up first, then upgrade it:\n"
            f"    mcuhome project upgrade {file.root}\n"
            "the upgrade explains what it changes before it does anything."
        ),
    )
