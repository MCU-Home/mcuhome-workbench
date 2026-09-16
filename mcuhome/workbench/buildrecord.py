# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""What a build directory holds, after the process that built it is gone.

A build answers a :class:`~mcuhome.workbench.build.BuildResult` to
whoever awaited it, and that is the whole story for a command line that
runs one build and exits. It is not the story for a client that comes
back: a dashboard restarts, a second process opens the same project, a
person runs ``mcuhome`` again tomorrow — and the only thing left of
yesterday's build is a directory. Without a record of it, such a client
can do one of two things, and both are wrong: build again to find out
what is there, or guess from file names.

So a build leaves one behind. :func:`build_firmware
<mcuhome.workbench.build.build_firmware>` writes
:data:`BUILD_RECORD_FILE` into the directory it was given when the build
ends — a build that failed and a build somebody stopped included, because
"there is nothing here" is an answer a client needs as much as the other
one. It is bookkeeping and therefore hidden and prefixed ``.mcuhome-``,
next to the lock file that guards the same directory, so that what the
user takes away is still the only thing with a plain name in there.

**Nothing here verifies anything.** :func:`read_build` states what the
record and the directory say, and re-computes no hash: the hashes it
answers are the ones the build measured when it delivered the artifacts.
A file somebody edited afterwards therefore still appears, with the hash
it had when it was built. That is deliberate — reading a directory to
show a person what is in it must not cost a gigabyte of hashing — and it
is the reason this module never speaks of a *verified* build. What
verifies a build context is
:func:`~mcuhome.workbench.contextdir.verify_context`; what verifies an
artifact is the step that consumes it.

**The record is not the source of truth, only the fast one.** A
directory a build never recorded — one built by an older version, one
somebody copied from a colleague — is read off the files themselves: the
build report and the firmware beside it. What only the record knows
(which device, which context, which image) is then empty rather than
guessed, because a guess in a record reads exactly like a fact.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcuhome.model.artifacts import Artifact, artifacts_from_wire
from mcuhome.model.errors import BuildError

from mcuhome.workbench.buildenvsession import ROOT_OUT
from mcuhome.workbench.buildlock import is_busy, open_build_lock
from mcuhome.workbench.imgtool import BUILD_REPORT_FILE, SIGNED_FIRMWARE_NAMES, SignedArtifact
from mcuhome.workbench.project import DEVICE_FILE
from mcuhome.workbench.projectfile import PROJECT_MARKER_FILE, UPGRADE_MARKER_FILE

if TYPE_CHECKING:  # pragma: no cover - the build layer imports this module, not the other way round
    from mcuhome.workbench.build import BuildResult

__all__ = [
    "BUILD_RECORD_FILE",
    "LOCAL_WORK_DIR",
    "REMOTE_WORK_DIR",
    "BuildRecord",
    "CleanResult",
    "clean_build",
    "read_build",
    "write_build_record",
]

#: What a build writes about itself into the directory it built in.
#: Hidden and prefixed like every file this package keeps for its own
#: bookkeeping inside a directory that belongs to the user.
BUILD_RECORD_FILE = ".mcuhome-build.json"

#: The record format this module writes and reads. A record under
#: another number is read by nobody and falls back to the files, which
#: is why a version is worth its one line: the alternative is a future
#: shape being half-understood by an older reader.
RECORD_VERSION = 1

#: Where a build keeps its scratch tree inside the directory it was given
#: — one name per target, spelled here because this module removes what
#: :mod:`…build` creates and the two must not drift apart. They are the
#: work roots of a build that was not given one; a caller that states
#: :attr:`BuildRequest.work_root` owns that directory and this never
#: touches it.
LOCAL_WORK_DIR = ".mcuhome-local"
REMOTE_WORK_DIR = ".mcuhome-remote"
WORK_DIRS = (LOCAL_WORK_DIR, REMOTE_WORK_DIR)


@dataclass(frozen=True)
class BuildRecord:
    """What a build directory holds, as far as anything can say without building.

    :attr:`out_dir` is where the build *delivered*: the report, the
    artifacts and anything signing wrote afterwards are in it. It is the
    directory :func:`read_build` was asked about wherever the build wrote
    there, and a directory inside it where a build environment delivered
    into one of its own.

    :attr:`artifacts` carries the hashes the build measured, not hashes
    of the files that are there now — see the module docstring. A field
    only the record can fill (:attr:`device`, :attr:`context_id`,
    :attr:`container_image`) is empty for a directory that holds no
    record.
    """

    #: Where the artifacts and the report are.
    out_dir: Path
    #: The device this was a build of, by its own name.
    device: str
    #: The identity the work was attributed to: the build context's ID.
    context_id: str
    #: What the build declared it delivered, as it declared it.
    artifacts: tuple[Artifact, ...]
    #: The build report's file name in :attr:`out_dir`.
    report: str
    #: The signed images, read off the directory: signing happens after
    #: the build, so no record of the build can know about them. Looked
    #: for at the top of the build directory first — where a client
    #: copies a build's output up for the user — and then beside the
    #: unsigned images in :attr:`out_dir`.
    signed: tuple[SignedArtifact, ...]
    #: The build environment that ran, where one did.
    container_image: str
    #: Whether somebody is working in the directory right now — a build,
    #: a signature, a clean. A record of a build in flight, not of a
    #: finished one.
    busy: bool

    def to_dict(self) -> dict[str, Any]:
        """This record as a document, JSON-ready and complete."""
        return {
            "out_dir": str(self.out_dir),
            "device": self.device,
            "context_id": self.context_id,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "report": self.report,
            "signed": [artifact.to_dict() for artifact in self.signed],
            "container_image": self.container_image,
            "busy": self.busy,
        }


def write_build_record(out_dir: Path, *, result: BuildResult) -> Path | None:
    """Write what *result* says into ``.mcuhome-build.json`` under *out_dir*.

    Called by :func:`~mcuhome.workbench.build.build_firmware` at the end
    of every build it ran — successful, failed or stopped — while it
    still holds the directory, so the record is written by the one party
    that may write there at that moment.

    **Best effort by design**: a directory that cannot be written to is
    not a reason to turn a finished build into a failure, so the answer
    is ``None`` instead of an exception. The build's own result is
    unaffected either way; what is lost is the next client's shortcut,
    and :func:`read_build` still has the files.
    """
    directory = Path(out_dir)
    document = {
        "build": RECORD_VERSION,
        "device": result.device,
        "context_id": result.context_id,
        # Where the files are, which is not always the directory this
        # record lies in: a build environment delivers into an output
        # directory of its own under the work root.
        "out_dir": None if result.out_dir is None else str(result.out_dir),
        "report": result.report,
        "container_image": result.container_image,
        "artifacts": [artifact.to_dict() for artifact in result.artifacts],
    }
    path = directory / BUILD_RECORD_FILE
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def _recorded(directory: Path) -> dict[str, Any] | None:
    """The record in *directory*, or ``None`` where there is none to use.

    A record that is missing, unreadable, not a document or written in a
    format version this module does not implement is all the same answer
    — there is nothing here to read from — and the caller falls back to
    the files. A refusal would be the wrong shape entirely: the record is
    this package's own bookkeeping, and a client asking what a directory
    holds gets told about the *build*, never about the bookkeeping.
    """
    try:
        data = json.loads((directory / BUILD_RECORD_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("build") != RECORD_VERSION:
        return None
    return data


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _signed_artifacts(*places: Path) -> tuple[SignedArtifact, ...]:
    """The signed images, by the names signing gives them, in the first place that has one.

    Two places, because signing happens after the build and its output
    ends up wherever the person who signed was working: beside the
    unsigned images in the delivery directory, or at the top of the build
    directory, where a client that copies a build's output up for the
    user keeps them. The order the caller states decides which one is
    answered for an encoding that exists in both; one entry per encoding
    either way, because they are the same image.
    """
    found: list[SignedArtifact] = []
    for _unsigned, signed in SIGNED_FIRMWARE_NAMES:
        for place in places:
            path = place / signed
            if path.is_file():
                found.append(SignedArtifact(format=Path(signed).suffix.lstrip("."), path=path))
                break
    return tuple(found)


def _delivered(directory: Path) -> tuple[Artifact, ...]:
    """What a directory nobody recorded appears to hold.

    The build report and the unsigned firmware beside it, under the
    artifact root a build delivers into — and **without a hash**, because
    nothing here measured one and a hash computed now would state as a
    fact what is merely the current content of the file.
    """
    found: list[Artifact] = []
    if (directory / BUILD_REPORT_FILE).is_file():
        found.append(Artifact(root=ROOT_OUT, path=BUILD_REPORT_FILE, role="report", sha256=""))
    for unsigned, _signed in SIGNED_FIRMWARE_NAMES:
        if (directory / unsigned).is_file():
            found.append(Artifact(root=ROOT_OUT, path=unsigned, role="firmware", sha256=""))
    return tuple(found)


def read_build(out_dir: Path) -> BuildRecord | None:
    """What the build directory *out_dir* holds, or ``None``.

    The answer a client needs when it comes back to a directory instead
    of having just built in it: which device, which context, which
    artifacts, whether they are signed, and whether somebody is working
    in there right now. The record a build left is read first; a
    directory that holds none is read off its files, and a directory that
    holds neither is ``None`` — an empty one, one that does not exist,
    one that was never a build directory.

    Hashes are **not** re-computed and the artifacts are not checked for
    existence: this states what the build said it delivered. A file
    replaced afterwards appears exactly as it was declared, which is why
    nothing here calls a build verified.
    """
    directory = Path(out_dir)
    if not directory.is_dir():
        return None
    busy = is_busy(directory)
    data = _recorded(directory)
    if data is not None:
        delivery = _text(data, "out_dir")
        into = Path(delivery) if delivery else directory
        return BuildRecord(
            out_dir=into,
            device=_text(data, "device"),
            context_id=_text(data, "context_id"),
            artifacts=artifacts_from_wire(data.get("artifacts") or ()),
            report=_text(data, "report"),
            # The directory that was asked about first: what a client
            # copied up for the user is the copy a person signs and
            # flashes, and it is the one they would be shown.
            signed=_signed_artifacts(directory, into),
            container_image=_text(data, "container_image"),
            busy=busy,
        )
    artifacts = _delivered(directory)
    signed = _signed_artifacts(directory)
    if not artifacts and not signed:
        return None
    return BuildRecord(
        out_dir=directory,
        device="",
        context_id="",
        artifacts=artifacts,
        report=BUILD_REPORT_FILE if (directory / BUILD_REPORT_FILE).is_file() else "",
        signed=signed,
        container_image="",
        busy=busy,
    )


def _inside(path: Path, directory: Path) -> bool:
    """Whether *path* is below *directory*, without resolving symlinks.

    A record is a file, and a file can be edited: a path in one that
    climbs out of the build directory is answered with "not mine to
    remove" rather than with a deletion somewhere else on the disk.
    """
    try:
        return path != directory and path.is_relative_to(directory)
    except ValueError:  # pragma: no cover - is_relative_to answers False instead
        return False


def _removals(directory: Path) -> list[Path]:
    """Every path in *directory* a build put there, in no particular order.

    **By name, every one of them**, and never by a pattern: an earlier
    version swept every hidden ``.mcuhome-*`` entry, which is the rule
    that says which files are this package's — and aimed at a project
    root it would have taken the project marker with it. What a build
    writes is a short, known list, and a list is the only thing that
    cannot grow teeth when a later version puts another file somewhere.
    """
    found: list[Path] = [directory / BUILD_RECORD_FILE, directory / BUILD_REPORT_FILE]
    found += [directory / name for name in WORK_DIRS]
    data = _recorded(directory)
    delivery = Path(_text(data, "out_dir") or directory) if data is not None else directory
    for artifact in artifacts_from_wire((data or {}).get("artifacts") or ()):
        found.append(delivery / artifact.path)
    # Both places, because the report and the firmware are a build's
    # output wherever they lie: the delivery directory the build wrote
    # into, and the top of the build directory, where a client that
    # copied them up for the user keeps them.
    for where in (directory, delivery):
        found += [where / BUILD_REPORT_FILE]
        for unsigned, signed in SIGNED_FIRMWARE_NAMES:
            found += [where / unsigned, where / signed]
    return [path for path in found if _inside(path, directory)]


def _refuse_a_directory_of_theirs(directory: Path) -> None:
    """Refuse to clean a project root or a device folder.

    ``clean_build`` deletes, and the one mistake it must not carry out is
    being aimed one directory too high: a project root and a device
    folder both hold files whose names this package knows, and a caller
    that passes one meant a build directory. Saying so costs two
    ``stat`` calls and saves somebody's project.
    """
    for marker in (PROJECT_MARKER_FILE, UPGRADE_MARKER_FILE):
        if (directory / marker).exists():
            raise BuildError(
                f"{directory} is an MCUHome project, not a build directory.",
                hint=(
                    "cleaning removes what a build produced, and a build writes into "
                    "build/<device>/ inside the project. Name that directory instead — "
                    "nothing here was removed."
                ),
            )
    if (directory / DEVICE_FILE).is_file():
        raise BuildError(
            f"{directory} holds a device configuration ({DEVICE_FILE}), "
            "so it is a device folder rather than a build directory.",
            hint=(
                "cleaning removes what a build produced; a device's own folder holds "
                "what you wrote. The build output of a device is in build/<device>/ — "
                "nothing here was removed."
            ),
        )


@dataclass(frozen=True)
class CleanResult:
    """What one clean removed, and which directory it was.

    The bare list of paths :func:`clean_build` used to answer said
    nothing about *what* was cleaned, so every client that printed the
    act had to write the sentence around it — which device, which
    directory — and two clients would have written two. It carries what
    the call already knows and nothing else.
    """

    #: The device the directory belongs to, as the caller named it;
    #: empty for a directory that was cleaned by path alone.
    device: str
    #: The build directory this cleaned.
    out_dir: Path
    #: Every path that went, sorted, and empty where there was nothing
    #: to remove.
    removed: tuple[Path, ...]

    def to_dict(self) -> dict[str, Any]:
        """This clean as a document, JSON-ready and complete."""
        return {
            "device": self.device,
            "out_dir": str(self.out_dir),
            "removed": [str(path) for path in self.removed],
        }


def clean_build(out_dir: Path, *, device: str = "") -> CleanResult:
    """Remove what a build wrote into *out_dir*, and answer what went.

    The build directory itself stays, and so does everything in it that a
    build did not write. What goes is named one by one and nothing else
    is: the build record, the build report, the artifacts the record
    declares, the unsigned and signed firmware beside them — in the
    delivery directory and at the top of the build directory, because a
    client may have copied them up — and the two work roots a build
    creates when it was not given one (``.mcuhome-local``,
    ``.mcuhome-remote``). A file somebody put there themselves is not a
    build's leftover, however much it looks like one, and neither is a
    work root the *caller* named: that directory is theirs.

    The **lock file stays**, deliberately: it is the guard this call is
    holding, and unlinking a path another process has already opened
    hands out two exclusive locks on two inodes under one name (see
    :mod:`~mcuhome.workbench.buildlock`).

    The directory is **held** for the duration, under the ``clean``
    operation, so a build or a signature that is running there refuses
    this one in words (:class:`~mcuhome.workbench.buildlock.BuildDirectoryBusy`)
    rather than losing its output half-way through. *device* is what the
    refusal calls the thing being cleaned, for whoever meets it, and it
    travels on into the :class:`CleanResult` this answers: a client that
    prints the act says which device and which directory without
    composing the sentence out of its own arguments.

    A **project root or a device folder is refused** with a
    :class:`~mcuhome.model.errors.BuildError` before anything is removed:
    both hold files this package knows by name, and a caller that hands
    one over meant a build directory.

    A directory that does not exist is answered with a result that
    removed nothing, and is **not** created: there was nothing there to
    remove, and a clean that leaves a new empty directory behind has done
    the opposite of its job.
    """
    directory = Path(out_dir)
    if not directory.is_dir():
        return CleanResult(device=device, out_dir=directory, removed=())
    _refuse_a_directory_of_theirs(directory)
    removed: list[Path] = []
    with open_build_lock(directory, device=device, operation="clean"):
        for path in _removals(directory):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_symlink() or path.exists():
                with contextlib.suppress(OSError):
                    path.unlink()
            else:
                continue
            if not path.exists():
                removed.append(path)
    return CleanResult(device=device, out_dir=directory, removed=tuple(sorted(set(removed))))
