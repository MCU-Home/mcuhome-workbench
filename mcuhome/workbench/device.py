# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Renaming a device of a project, and deleting one.

A device of a project is a folder under ``devices/`` with a
``main.yaml`` in it, and that folder's name **is** the device: the build
directory, the build lock, the result documents, the OTA image, the
per-device secrets, the pairing credentials and the patches a build
carries are all keyed on it, and a device file that calls itself
something else is refused when it is loaded
(:func:`~mcuhome.workbench.loader.require_folder_name`). So renaming a
device is not an edit of one line. It is the folder, the name written
inside it, and the per-device files the project keeps elsewhere — and a
rename that moved only some of them would leave one device answering to
one name and its credentials to another.

That is what these two calls are for, and why they are not something a
client assembles out of file operations of its own.

**A rename removes the device's build output.** Build output names the
device inside its own report, its record and its signed images, so a
build directory carried over to the new name would describe a device
that no longer exists — and the first person to read that report would
believe it. Removing it is the honest end of a rename: build output is
disposable, the next build rewrites all of it, and the alternative is a
directory that lies. :func:`delete_device` removes it for the same
reason plus the obvious one.

**A delete takes the device's secrets with it** unless the caller says
otherwise. A device's commissioning credentials are drawn once and never
again: keeping them is what a user wants who is deleting a device in
order to re-create it, and losing them is what they want who is giving
the hardware away. Neither is a default this package can guess for them,
so *keep_secrets* is stated at the call and the answer lists what went.

**Neither of them reads a secrets file**, so neither runs the
permission guard the secrets surface runs before every read
(:func:`~mcuhome.workbench.project.require_secret_file`): a device whose
secrets file is readable by other users is renamed or deleted rather
than refused. Refusing would leave the file exactly where it is, and
moving it — or removing it — is the outcome that ends the exposure.

**Both hold the device's build directory** for the whole operation,
under the ``rename`` and ``delete`` operations of
:data:`~mcuhome.workbench.buildlock.LOCK_OPERATIONS`. A build, a
signature or a flash that is running there refuses this one in words
(:class:`~mcuhome.workbench.buildlock.BuildDirectoryBusy`) instead of
finding its directory gone half-way through. Which is why the directory
is *emptied* under the lock and only removed after it is released
(:func:`_empty_build_dir`,
:func:`~mcuhome.workbench.buildlock.discard_build_directory`): the lock
file is what the exclusion rests on, and removing it along with the
build output would leave the path free for a second process to create
another one under the same name and start working — in the build
directory of a device this call is still moving.

**Neither leaves a trace of itself.** Holding a build directory creates
it, and a device that was never built has none — so both calls remove
the directories they only took in order to hold them, and the project's
``build/`` as well when it was this call that brought it into existence.
A ``build/`` that was already there stays: it is the project's, not
this call's to tidy away.

**What the order of operations promises.** Everything that can be
refused is refused before the first file is touched, and the new device
file is rendered in memory at that point too — so a device whose YAML
this package cannot read is refused rather than half-moved. After that
the build output goes first (it is disposable, and removing it can be
retried), then the device folder, then the file inside it, then the
secrets file. A failure after the folder has moved is therefore a
refusal that names what did move and the one command that finishes the
job, rather than a project nothing can find its way around in. The one
exception is the name: a folder that moved and a file that could not be
rewritten is the single state that would not *load*, so that one step is
taken back rather than reported, and the refusal says the device is as it
was.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from mcuhome.model.errors import ConfigError, Location

from mcuhome.workbench import schema
from mcuhome.workbench.buildlock import (
    BUILD_LOCK_FILE,
    discard_build_directory,
    open_build_lock,
)
from mcuhome.workbench.loader import editing_yaml, read_editable_yaml
from mcuhome.workbench.project import BUILD_DIR, DEVICE_FILE, Project, refuse_unknown_device

__all__ = [
    "DeleteResult",
    "RenameResult",
    "delete_device",
    "rename_device",
]


@dataclass(frozen=True)
class RenameResult:
    """What one rename moved, and what the device is called now.

    A bare list of paths said nothing about the act it came out of, so
    every client that printed a rename had to state the two names
    itself. They are what the call was given; carrying them back is what
    lets one document describe the whole thing.
    """

    #: The name the device had.
    device: str
    #: The name it has now.
    to: str
    #: Every path this changed, in the order it changed them.
    changed: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        """This rename as a document, JSON-ready and complete."""
        return {
            "device": self.device,
            "to": self.to,
            "changed": [str(path) for path in self.changed],
        }


@dataclass(frozen=True)
class DeleteResult:
    """What one delete removed, and whether the credentials stayed.

    :attr:`kept_secrets` is the caller's own statement read back, and it
    is in the document because it is the one thing about a delete that
    cannot be seen from what went: a device whose secrets file was kept
    and one that never had it answer the same list of paths.
    """

    #: The device that is gone.
    device: str
    #: Whether its commissioning credentials were kept, as asked.
    kept_secrets: bool
    #: Every path this removed, in the order it removed them.
    removed: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        """This delete as a document, JSON-ready and complete."""
        return {
            "device": self.device,
            "kept_secrets": self.kept_secrets,
            "removed": [str(path) for path in self.removed],
        }


def _require_device(project: Project, name: str) -> Path:
    """The device folder of *name*, or the refusal that lists what is there."""
    if not project.device_file(name).is_file():
        raise refuse_unknown_device(project, name)
    return project.devices_dir / name


def _exists(path: Path) -> bool:
    """Whether anything is at *path* — a broken symlink included.

    A name is taken by whatever occupies it, and a link pointing
    nowhere occupies it as much as a directory does: moving onto one
    would follow it and write outside the project.
    """
    return path.exists() or path.is_symlink()


def _require_free(name: str, path: Path, what: str) -> None:
    """Refuse a target name something is already using."""
    if not _exists(path):
        return
    raise ConfigError(
        f'The name "{name}" is taken: {path} is already there.',
        location=Location(file=path),
        hint=(
            f"pick another name, or remove the {what} first — MCUHome never renames "
            "a device onto something that already exists."
        ),
    )


def _discard_build_root(project: Project, *, created: bool) -> None:
    """Remove the ``build/`` this call brought into existence, if it is empty.

    Taking a lock creates the directory it guards, and the directory it
    guards is inside ``build/`` — so a rename or a delete of a device
    that was never built would otherwise leave a ``build/`` behind that
    nobody asked for. What this call created, this call removes; a
    ``build/`` that was already there is the project's, and stays even
    when the last device's output has just gone out of it.
    """
    if not created:
        return
    with contextlib.suppress(OSError):
        # Fails, and is meant to, while anything is still in it.
        (project.root / BUILD_DIR).rmdir()


def _discard(project: Project, *directories: Path, build_root: bool) -> None:
    """Take the emptied build directories away, whatever the call did.

    Run on the way out of a refusal as much as after a rename that
    worked, because a directory that was held and emptied and then left
    standing holds nothing but a lock file — and the next call to look
    at that name, which after a failure is most likely the rename back,
    would refuse it as a name that is taken.

    Removes nothing a run has taken in the meantime and nothing that
    still holds build output
    (:func:`~mcuhome.workbench.buildlock.discard_build_directory`), and
    raises nothing: it runs while an exception is on its way out, and an
    exception raised in there would replace the refusal the caller has
    to read.
    """
    for directory in directories:
        discard_build_directory(directory)
    _discard_build_root(project, created=build_root)


def _require_build_dir(path: Path) -> None:
    """Refuse a build directory that is not one, before the lock is taken.

    Two things are not one, and both would otherwise go wrong quietly.
    A **file** — or a link pointing nowhere — meets the ``mkdir`` that
    taking the lock does and comes back as a bare ``FileExistsError``
    from the standard library, in the middle of a call that refuses in
    words everywhere else. A **link to a directory** is worse than that,
    because it works: this call would remove the contents of whatever it
    points at, somewhere else on the disk, and then report the link as
    the build directory it removed. Somebody put that link there on
    purpose; where their build output goes is not this call's to decide,
    so it says so and does nothing.
    """
    if path.is_symlink():
        raise ConfigError(
            f"{path} is a link, and a device's build directory is a directory.",
            location=Location(file=path),
            hint=(
                "MCUHome will not remove a device's build output through a link "
                "somebody put in the project. Take the link away — and the directory "
                "it points at, if that is what you meant — then run this again."
            ),
        )
    if not path.exists() or path.is_dir():
        return
    raise ConfigError(
        f"MCUHome cannot use {path} as a build directory: it is not a directory.",
        location=Location(file=path),
        hint=(
            "a device's build output lives in build/<device>/ — move that file out "
            "of the way, then run this again"
        ),
    )


def _renamed_text(entry: Path, *, to: str) -> str | None:
    """The device file with ``device.name`` set to *to*, or ``None``.

    ``None`` when the file states no name at all: there is nothing to
    bring in line then, and writing one in would be this call inventing
    a key the user did not have. Everything else in the file — comments,
    order, quoting, ``!secret`` and ``!file`` tags — survives, because
    the file is parsed with the editing parse and written back with it;
    the name is the one thing a rename changes.
    """
    data = read_editable_yaml(entry)
    device = data.get("device") if isinstance(data, dict) else None
    if not isinstance(device, dict) or "name" not in device:
        return None
    device["name"] = to
    buffer = StringIO()
    editing_yaml().dump(data, buffer)
    return buffer.getvalue()


def _replace_atomically(target: Path, text: str) -> None:
    """Put *text* where *target* is — all of it or none of it.

    The device file is the user's own and the only statement of what
    this device is, so a write that is interrupted must not be able to
    leave half of it. The text goes into a temporary file in the same
    directory, is flushed, takes the mode the file already had and is
    moved over it in one step.

    A symlink is **followed** rather than replaced — the file somebody
    pointed at is the file that gets written, which is how every other
    write in this package treats one — and resolving it first is also
    what keeps the temporary file on the target's own filesystem, so the
    move stays a rename.
    """
    target = target.resolve()
    mode = stat.S_IMODE(target.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=".mcuhome-device-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _remove_tree(path: Path) -> None:
    """Remove a directory this package owns, link or not."""
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _empty_build_dir(directory: Path) -> None:
    """Remove everything a build left in *directory*, the lock file apart.

    The lock file is what holds the directory against everybody else
    while this runs, so it is the one thing that must not go here:
    unlink it and a second process opening the same path creates a
    second inode and is granted a second "exclusive" lock under one name
    — which is how a build starts in a directory somebody is half-way
    through renaming. The emptied directory itself goes afterwards, once
    the lock is released
    (:func:`~mcuhome.workbench.buildlock.discard_build_directory`).
    """
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.name == BUILD_LOCK_FILE:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _refuse_unremovable(what: str, path: Path, error: OSError) -> ConfigError:
    return ConfigError(
        f"MCUHome cannot remove the {what} {path}: {error.strerror or error}.",
        location=Location(file=path),
        hint="check who owns it and what still has it open, then try again",
    )


def rename_device(name: str, *, project: Project, to: str) -> RenameResult:
    """Rename the device *name* of *project* to *to*, or change nothing.

    Moves ``devices/<name>/`` to ``devices/<to>/`` — the patches, the
    generated tree and anything else the user keeps beside the device
    file travel with it — writes *to* into the moved file's
    ``device.name``, moves the device's secrets file, and **removes** the
    device's build directory. The module docstring says why the build
    output goes rather than moves, and in which order all of this
    happens.

    Answers a :class:`RenameResult`: the two names, and every path it
    changed in the order it changed them — the
    build directory that was removed (only when the device had one), the
    device folder under its new name, the device file where the name was
    rewritten (only when the file stated one), and the device's secrets
    file under its new name (only when there was one). The device's
    build directory under the *new* name is held for the operation as
    well, and is not answered: it is created by taking the lock and
    removed again, so a run that started there between the refusal and
    the move refuses in words rather than being moved onto.

    Raises :class:`~mcuhome.model.errors.ConfigError` for a device the
    project does not have, a *to* that is not a usable device name, a
    *to* the device already carries, a *to* some file or directory of
    this project is already using, a device file this package cannot
    parse, and a build directory that is not a directory — none of which
    touches anything.
    :class:`~mcuhome.workbench.buildlock.BuildDirectoryBusy` when another
    process is working in either build directory.
    """
    folder = _require_device(project, name)
    if to == name:
        raise ConfigError(
            f'The device "{name}" is already called that.',
            hint="rename it to a name it does not have yet, or leave it as it is",
        )
    if not schema.is_device_name(to):
        raise schema.refuse_device_name(to)

    target = project.devices_dir / to
    secrets = project.device_secrets_file(name)
    new_secrets = project.device_secrets_file(to)
    build_dir = project.device_build_dir(name)
    new_build_dir = project.device_build_dir(to)
    _require_free(to, target, "device folder")
    _require_free(to, new_secrets, "secrets file")
    _require_free(to, new_build_dir, "build directory")
    _require_build_dir(build_dir)

    text = _renamed_text(project.device_file(name), to=to)
    had_build = build_dir.is_dir()
    made_build_root = not (project.root / BUILD_DIR).exists()
    changed: list[Path] = []

    try:
        with (
            open_build_lock(build_dir, device=name, operation="rename"),
            open_build_lock(new_build_dir, device=to, operation="rename"),
        ):
            for directory in (build_dir, new_build_dir):
                try:
                    _empty_build_dir(directory)
                except OSError as error:
                    # Named one by one: a message that always said the
                    # device's own would send somebody to the wrong path.
                    raise _refuse_unremovable("build directory", directory, error) from error
            if had_build:
                changed.append(build_dir)

            try:
                os.rename(folder, target)
            except OSError as error:
                raise _refuse_unmovable_folder(folder, target, error) from error
            changed.append(target)

            entry = target / DEVICE_FILE
            if text is not None:
                try:
                    _replace_atomically(entry, text)
                except OSError as error:
                    raise _refuse_name_not_rewritten(
                        folder, target, name=name, to=to, error=error
                    ) from error
                changed.append(entry)

            if _exists(secrets):
                try:
                    # No directory to create: the two names are two files
                    # of one directory, and the old one is in it.
                    os.rename(secrets, new_secrets)
                except OSError as error:
                    raise _refuse_secrets_left(secrets, new_secrets, error) from error
                changed.append(new_secrets)
    finally:
        # On the way out of a refusal too: a directory held and emptied
        # and then left behind holds nothing but a lock file, and the
        # next call — renaming back, most likely — would refuse it as a
        # name that is taken.
        _discard(project, build_dir, new_build_dir, build_root=made_build_root)
    return RenameResult(device=name, to=to, changed=tuple(changed))


def _refuse_unmovable_folder(folder: Path, target: Path, error: OSError) -> ConfigError:
    """The folder could not be moved — and nothing else had happened yet."""
    return ConfigError(
        f"MCUHome cannot move {folder} to {target}: {error.strerror or error}.",
        location=Location(file=folder),
        hint=(
            "check who owns the devices directory and what still has the folder open. "
            "The device itself is untouched; only its build output was removed, and "
            "the next build writes that again."
        ),
    )


def _refuse_name_not_rewritten(
    folder: Path, target: Path, *, name: str, to: str, error: OSError
) -> ConfigError:
    """The folder moved and the name inside it did not.

    The one half-done state that would not load: a device file in a
    folder it disagrees with is refused by the loader, so this is the
    one step that is **taken back** rather than reported. The folder goes
    to where it came from — nothing else has happened yet, the file was
    never written and the secrets have not moved — and the refusal then
    says the device is as it was. Only when that move fails too is there
    something for a person to finish, and then the message says which
    line, in which file.
    """
    try:
        os.rename(target, folder)
    except OSError:
        entry = target / DEVICE_FILE
        return ConfigError(
            f'The device folder moved to {target}, and its file still says "{name}": '
            f"{error.strerror or error}.",
            location=Location(file=entry, key="device.name"),
            hint=(
                f"MCUHome refuses to load a device whose file disagrees with its folder. "
                f"Finish it by hand — in {entry}:\n"
                f"    device:\n"
                f"      name: {to}"
            ),
        )
    entry = folder / DEVICE_FILE
    return ConfigError(
        f"MCUHome cannot write the new name into {entry}: {error.strerror or error}.",
        location=Location(file=entry, key="device.name"),
        hint=(
            f'The device is still called "{name}" and nothing of it moved; only its '
            "build output was removed, and the next build writes that again. Fix the "
            "file's permissions and run the rename again."
        ),
    )


def _refuse_secrets_left(secrets: Path, new_secrets: Path, error: OSError) -> ConfigError:
    """The device moved and its secrets file did not — say exactly that."""
    return ConfigError(
        f"The device was renamed, and its secrets file {secrets} could not be moved: "
        f"{error.strerror or error}.",
        location=Location(file=secrets),
        hint=(
            "the device now has no secrets of its own — its commissioning credentials "
            "are still in that file. Move it by hand:\n"
            f"    mv {secrets} {new_secrets}"
        ),
    )


def delete_device(name: str, *, project: Project, keep_secrets: bool = False) -> DeleteResult:
    """Delete the device *name* of *project*, or change nothing.

    Removes the device's build directory, then ``devices/<name>/`` with
    everything in it, then the device's secrets file — that last one
    unless *keep_secrets*, which is how a caller keeps commissioning
    credentials a controller already knows and that cannot be drawn
    again.

    Answers a :class:`DeleteResult`: the device, whether its credentials
    were kept, and every path it removed in the order it removed them —
    the build directory (only when the device had one), the device
    folder, and the secrets file (only when there was one and it was not
    kept).

    Raises :class:`~mcuhome.model.errors.ConfigError` for a device the
    project does not have and for a build directory that is not a
    directory, neither of which touches anything, and
    :class:`~mcuhome.workbench.buildlock.BuildDirectoryBusy` when another
    process is working in the device's build directory.
    """
    folder = _require_device(project, name)
    secrets = project.device_secrets_file(name)
    build_dir = project.device_build_dir(name)
    _require_build_dir(build_dir)
    had_build = build_dir.is_dir()
    made_build_root = not (project.root / BUILD_DIR).exists()
    removed: list[Path] = []

    try:
        with open_build_lock(build_dir, device=name, operation="delete"):
            try:
                _empty_build_dir(build_dir)
            except OSError as error:
                raise _refuse_unremovable("build directory", build_dir, error) from error
            if had_build:
                removed.append(build_dir)

            try:
                _remove_tree(folder)
            except OSError as error:
                raise _refuse_unremovable("device folder", folder, error) from error
            removed.append(folder)

            if not keep_secrets and _exists(secrets):
                try:
                    secrets.unlink()
                except OSError as error:
                    raise _refuse_unremovable("secrets file", secrets, error) from error
                removed.append(secrets)
    finally:
        _discard(project, build_dir, build_root=made_build_root)
    return DeleteResult(device=name, kept_secrets=keep_secrets, removed=tuple(removed))
