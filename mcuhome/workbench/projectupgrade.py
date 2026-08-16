# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Upgrading a project to the current layout (PO 2026-08-16).

A project states which layout it speaks
(:mod:`mcuhome.workbench.projectfile`), and when MCUHome moves past that
layout every command refuses until the project has been upgraded. This
module is the upgrade: it applies the migrations of
:mod:`mcuhome.workbench.migrations` in order, one version step at a time.

**The project file is renamed for the whole run.** Before anything is
migrated the file becomes ``.mcuhome-project-root.upgrade``, and it stays
that way until the last migration is done. That single move does three
jobs at once:

* nothing else can start work on the project — a build, a flash, a
  second upgrade all resolve the project through the marker, and while
  it is not there they refuse rather than run against a layout that is
  being rewritten under them;
* it makes a second upgrade impossible without any lock protocol of its
  own: the rename is atomic, so of two upgraders exactly one finds the
  file to rename and the other is told what is going on;
* what a person sees afterwards is honest. An upgrade that was killed
  outright leaves the file renamed, and every command then says *this
  project was left mid-upgrade* instead of pretending the project is
  fine.

The **lock** on top of the rename answers one question the rename cannot:
whether the process that renamed the file is still alive. It is
:func:`fcntl.flock` on the file itself, held across the rename (a lock
belongs to the file, not to its name), so an upgrade that is running now
and an upgrade that died are two different messages instead of one vague
one.

**There is no resumption.** A migration that fails leaves the project
renamed and unusable, and the supported way out is the backup the
upgrade told the user to make. Re-running half a migration would need
every migration to be exactly repeatable at every point inside itself —
a promise no migration can make and a machinery whose own bugs would
cost more than they save (PO 2026-08-16). A *clean* stop is the
exception, and it is not a resumption: when the caller asks to stop, the
current migration runs to its end, the version reached is written, and
the file is renamed back. The project is then simply a project of that
version, and the next upgrade starts there like any other.

**Waiting, not locking, for builds.** Once the marker is gone no build
can start, so the upgrade takes no build-directory lock; it only asks
which build directories are still busy (:func:`running_builds`) so the
caller can wait for a run that started earlier. The caller's own
confirmation belongs *after* that wait — a person who says "yes" must
see the upgrade begin, not a wait they might interrupt at the exact
moment it ends.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from mcuhome.model.errors import ConfigError, MCUHomeError

from mcuhome.workbench.buildlock import holder_of, is_busy
from mcuhome.workbench.migrations import Migration, plan_for
from mcuhome.workbench.projectfile import (
    MARKER_FILE,
    PROJECT_VERSION,
    UPGRADE_FILE,
    ProjectFile,
    UpgradeRecord,
    read_project_file,
    write_project_file,
)

try:  # pragma: no cover - the import itself is the platform check
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "MigrationFailed",
    "RunningBuild",
    "UpgradeInterrupted",
    "UpgradeInProgress",
    "UpgradeResult",
    "UpgradeSession",
    "is_upgrading",
    "running_builds",
    "upgrade_session",
]

#: Where builds put their directories, one per device (ADR 0022 §1).
BUILD_DIR = "build"


class UpgradeInProgress(ConfigError):
    """Another process is upgrading this project right now."""


class UpgradeInterrupted(ConfigError):
    """An upgrade of this project was killed; the project is not usable."""


class MigrationFailed(ConfigError):
    """A migration stopped part-way. The project is left mid-upgrade."""


@dataclass(frozen=True)
class RunningBuild:
    """A build directory of this project that is still busy."""

    directory: Path
    device: str = ""
    operation: str = ""
    process: str = ""
    started: str = ""

    @property
    def name(self) -> str:
        return self.device or self.directory.name


@dataclass(frozen=True)
class UpgradeResult:
    """What one upgrade did."""

    from_version: int
    to_version: int
    applied: tuple[Migration, ...]
    #: True when the caller asked to stop and migrations were left over.
    stopped: bool = False

    @property
    def remaining(self) -> tuple[Migration, ...]:
        return plan_for(self.to_version)


def is_upgrading(root: Path) -> bool:
    """Whether a *live* process is upgrading the project in *root*.

    False for a leftover from an upgrade that was killed — that file has
    no owner, and the kernel is what says so.
    """
    path = Path(root) / UPGRADE_FILE
    if fcntl is None or not path.is_file():  # pragma: no cover - platform branch
        return False
    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(handle)
    return False


def in_flight_error(root: Path) -> ConfigError:
    """The refusal every command gives for a project that is mid-upgrade.

    Two cases, and telling them apart is the point: an upgrade that is
    running (wait for it) and an upgrade that was killed (the project is
    damaged; restore the backup).
    """
    root = Path(root)
    path = root / UPGRADE_FILE
    try:
        record = read_project_file(path, root=root).upgrade or UpgradeRecord()
    except MCUHomeError:
        record = UpgradeRecord()
    since = f", started {record.started}" if record.started else ""
    who = f" (process {record.process}{since})" if record.process else since
    if is_upgrading(root):
        return UpgradeInProgress(
            f'The project in "{root}" is being upgraded right now{who}.',
            hint="wait for that upgrade to finish, then run this command again.",
        )
    at = f" while applying {record.running}" if record.running else ""
    return UpgradeInterrupted(
        f'The upgrade of the project in "{root}" was interrupted{at}.',
        hint=(
            "the project is in a half-migrated state and MCUHome will not guess at "
            "it. Restore the backup you made before the upgrade and run it again."
        ),
    )


def running_builds(root: Path) -> tuple[RunningBuild, ...]:
    """Which of this project's build directories are busy right now.

    Asked, not held — see the module docstring. Only the project's own
    ``build/`` is looked at: a build somewhere else was pointed there by
    hand, and the upgrade has no way to know about it.
    """
    directory = Path(root) / BUILD_DIR
    if not directory.is_dir():
        return ()
    busy = []
    for candidate in sorted(entry for entry in directory.iterdir() if entry.is_dir()):
        if not is_busy(candidate):
            continue
        holder = holder_of(candidate)
        busy.append(
            RunningBuild(
                directory=candidate,
                device=holder.get("device", ""),
                operation=holder.get("operation", ""),
                process=holder.get("pid", ""),
                started=holder.get("started", ""),
            )
        )
    return tuple(busy)


class UpgradeSession:
    """One upgrade, from the moment the project file is renamed.

    Created by :func:`upgrade_session`, which owns the rename and the
    lock. The session itself is what a caller drives: it knows the plan,
    answers which builds are still running, and applies the migrations
    when the caller says so.
    """

    def __init__(self, root: Path, file: ProjectFile, path: Path, started: str) -> None:
        self.root = root
        #: The project as it stands right now — its version rises as
        #: migrations are applied.
        self.file = file
        self.path = path
        self.plan = plan_for(file.version)
        self.from_version = file.version
        #: Set when a migration failed: the file then stays renamed.
        self.failed: Migration | None = None
        # Local time: this record is read by a person next to the
        # machine (same rule as the build lock's).
        self._started = started

    def running_builds(self) -> tuple[RunningBuild, ...]:
        """Which build directories are still busy — for the caller's wait."""
        return running_builds(self.root)

    def apply(
        self,
        *,
        on_event: Callable[[str, Migration], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> UpgradeResult:
        """Run the plan. *should_stop* is asked **between** migrations only.

        A stop request never cuts a migration in half: the one that is
        running finishes, its version is written, and the upgrade ends
        there cleanly. *on_event* is called with ``"start"`` and
        ``"done"`` and the migration each refers to.
        """
        applied: list[Migration] = []
        stopped = False
        for migration in self.plan:
            if should_stop is not None and should_stop():
                stopped = True
                break
            self._write(replace(self.file, upgrade=self._record(migration.name)))
            if on_event is not None:
                on_event("start", migration)
            try:
                produced = migration.run(self.root, self.file)
            except MCUHomeError as error:
                self.failed = migration
                raise self._failure(migration, str(error)) from error
            except Exception as error:  # noqa: BLE001 - every failure reads the same
                self.failed = migration
                raise self._failure(migration, f"{type(error).__name__}: {error}") from error
            self.file = replace(produced, version=migration.to_version)
            self._write(replace(self.file, upgrade=self._record("")))
            applied.append(migration)
            if on_event is not None:
                on_event("done", migration)
        return UpgradeResult(
            from_version=self.from_version,
            to_version=self.file.version,
            applied=tuple(applied),
            stopped=stopped,
        )

    def _record(self, running: str) -> UpgradeRecord:
        return UpgradeRecord(
            started=self._started,
            process=os.getpid(),
            host=socket.gethostname(),
            running=running,
        )

    def _write(self, file: ProjectFile) -> None:
        write_project_file(self.path, file)

    def _failure(self, migration: Migration, reason: str) -> MigrationFailed:
        return MigrationFailed(
            f'The project in "{self.root}" could not be upgraded: the migration '
            f'"{migration.name}" ({migration.description}) stopped with: {reason}',
            hint=(
                "the project is left in a half-migrated state and is not usable. "
                "Restore the backup you made before the upgrade; MCUHome does not "
                "repair a partly applied migration."
            ),
        )


@contextmanager
def upgrade_session(root: Path) -> Iterator[UpgradeSession]:
    """Take the project in *root* for an upgrade: rename it, lock it, yield.

    On the way in the project file is locked and renamed to
    :data:`~mcuhome.workbench.projectfile.UPGRADE_FILE`; on the way out
    it is renamed back — unless a migration failed, in which case it
    deliberately stays renamed and the project stays refused (see the
    module docstring). Every other exit renames back, including a
    caller that declines, aborts or raises before applying anything.
    """
    root = Path(root).resolve()
    marker = root / MARKER_FILE
    working = root / UPGRADE_FILE

    if working.exists() and not marker.exists():
        raise in_flight_error(root)
    try:
        handle = os.open(marker, os.O_RDWR)
    except FileNotFoundError:
        raise ConfigError(
            f'"{root}" is not an MCUHome project directory: it has no {MARKER_FILE}.',
            hint="create a project there first with:\n    mcuhome project init",
        ) from None
    except OSError as error:
        raise ConfigError(
            f"The project file {marker} cannot be opened ({error.strerror or error}).",
            hint="an upgrade has to write it — check the file's owner and permissions.",
        ) from error

    try:
        if fcntl is not None:  # pragma: no branch - every supported platform
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise in_flight_error(root) from None
        file = read_project_file(marker, root=root)
        try:
            os.rename(marker, working)
        except OSError:
            # Someone renamed it between the open and here: they hold the
            # lock on the file we opened, so this is their upgrade.
            raise in_flight_error(root) from None
        session = UpgradeSession(
            root, file, working, datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        )
        # Who is upgrading, from when — written straight after the rename
        # rather than with the first migration, because the waiting and
        # the user's confirmation happen before that and a command that
        # bumps into the project meanwhile should name the run in the way.
        session._write(replace(file, upgrade=session._record("")))
        try:
            yield session
        finally:
            if session.failed is None:
                write_project_file(working, replace(session.file, upgrade=None))
                os.rename(working, marker)
    finally:
        os.close(handle)


def needs_upgrade(file: ProjectFile) -> bool:
    """Whether *file* is behind the layout these tools speak."""
    return file.version < PROJECT_VERSION
