# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""One build directory, one operation at a time (PO 2026-08-16).

A build directory is a working area, not an archive: every build method
wipes and rebuilds its scratch tree at the start of a run, because a
run's work belongs to that run and inheriting a dead one's leftovers is
how a build ends up compiling a mixture. That is right for a *finished*
neighbour and wrong for a *running* one — the second run deletes the
first one's generated headers mid-compile, and the first dies on
something like ``autoconf.h: No such file or directory``, a message that
says nothing about what actually happened. It was observed exactly that
way before this module existed.

**The guard is not about builds.** Everything MCUHome does to a device
after stage 5 reads or writes the files in that one directory: signing
replaces the application image, flashing reads it and writes it to
hardware, cleaning removes all of it. A build that overwrites
``firmware.signed.hex`` while it is being flashed is the same collision
with worse consequences — a device that receives half of one image and
half of another. So the lock is per **build directory** and every
operation on one takes it, naming what it is doing; the refusal then
says which run is in the way and what it is up to.

**Why an OS lock and not a PID file.** The lock is :func:`fcntl.flock`
on ``.mcuhome-build.lock`` in the build directory, which the kernel
releases when the holding process ends — however it ends. A crashed run
therefore leaves nothing to clean up and nothing to second-guess, which
is the whole failure mode a hand-rolled PID file gets wrong. The file's
*content* is only there to make the refusal useful (which process, since
when, which device, doing what); it is written after the lock is held,
read without one, and never trusted for the decision.

Within one process the lock is **re-entrant by count**: a command line
holds the directory for a whole ``device build`` — compile *and* the
host-side signing that follows it — and the build method it calls takes
the same lock again for embedders that call it directly. One process
running two operations on one directory at the same time is a program
error this cannot catch and the CLI cannot make; every consumer that
runs builds concurrently gives each one its own directory.

The lock file is deliberately **not deleted** on release. Unlinking a
path another process has already opened would hand out two "exclusive"
locks on two inodes under one name — an empty file left in a directory
full of build output is the cheaper end of that trade.

Platforms without POSIX advisory locks (``fcntl`` is absent on Windows)
get no lock, and are told so here rather than by a corrupted build: the
guard degrades to nothing, which is what it was before this module
existed. Every platform MCUHome builds on today has it.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from mcuhome.model.errors import BuildError

try:  # pragma: no cover - the import itself is the platform check
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "LOCK_FILE",
    "OPERATIONS",
    "BuildDirectoryBusy",
    "build_lock",
    "holder_of",
    "is_busy",
]

#: The lock file, inside the build directory it guards.
LOCK_FILE = ".mcuhome-build.lock"

#: What an operation is called in a refusal — "<device> is being …".
#: Append-only vocabulary: a word this version does not know is rendered
#: by the generic sentence rather than refused, so a newer caller (or a
#: newer command) never turns into a worse message.
OPERATIONS = {
    "build": "A build of {device} is already running",
    "sign": "{device} is being signed",
    "flash": "{device} is being flashed",
    "clean": "The build output of {device} is being deleted",
}

#: Paths this process already holds, and how deep. Guarded by
#: :data:`_COUNTS_LOCK`, because the count is read-modify-write state and
#: a build method may be driven from a worker thread.
_COUNTS: dict[Path, int] = {}
_COUNTS_LOCK = threading.Lock()


class BuildDirectoryBusy(BuildError):
    """Another MCUHome run is working in this build directory right now."""


def holder_of(out_dir: Path) -> dict[str, str]:
    """What the current holder wrote about itself, or ``{}``.

    Advisory in the strictest sense: the lock decides, this only makes
    the refusal readable. A half-written, empty or hand-edited file is
    answered with an empty record rather than an exception — a run is
    being refused either way, and the reason must not depend on prose.
    """
    try:
        data = json.loads((Path(out_dir) / LOCK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(key): str(value) for key, value in data.items()} if isinstance(data, dict) else {}


def is_busy(out_dir: Path) -> bool:
    """Whether someone is working in *out_dir* right now — without taking it.

    The question a caller asks when it wants to *wait* rather than
    refuse: the project upgrade holds no build directory (it has already
    made the project unfindable, so nothing new can start) and only needs
    to know whether a run that started earlier is still going. Tests the
    lock and drops it again immediately, and deliberately writes nothing
    — probing must not overwrite the holder record that makes somebody
    else's refusal readable.
    """
    path = Path(out_dir) / LOCK_FILE
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


@contextmanager
def build_lock(out_dir: Path, *, device: str = "", operation: str = "build") -> Iterator[None]:
    """Hold *out_dir* for one operation — ``build``, ``sign``, ``flash``, ``clean``.

    Raises :class:`BuildDirectoryBusy` — a typed refusal like any
    other — when another process is working there, naming what it is
    doing, which process it is and when it started, so a user can tell
    their own second terminal from something they forgot.
    """
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    with _COUNTS_LOCK:
        held = _COUNTS.get(out_dir, 0)
        if held:
            _COUNTS[out_dir] = held + 1
    if held:
        # Already ours: the outermost holder owns the file, the record
        # and the release (see the module docstring on re-entrancy).
        try:
            yield
        finally:
            with _COUNTS_LOCK:
                _COUNTS[out_dir] -= 1
                if not _COUNTS[out_dir]:
                    del _COUNTS[out_dir]
        return
    if fcntl is None:  # pragma: no cover - no POSIX locks on this platform
        yield
        return
    path = out_dir / LOCK_FILE
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise _busy(out_dir, device, operation) from None
        os.ftruncate(handle, 0)
        record = {
            "pid": str(os.getpid()),
            "host": socket.gethostname(),
            "device": device,
            "operation": operation,
            # Local time, deliberately: this is read by a person next to
            # the machine, comparing it against the clock on their own
            # terminal, and never by another program.
            "started": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        }
        os.write(handle, json.dumps(record).encode("utf-8"))
        os.fsync(handle)
        with _COUNTS_LOCK:
            _COUNTS[out_dir] = 1
        yield
    finally:
        with _COUNTS_LOCK:
            if _COUNTS.get(out_dir) == 1:
                del _COUNTS[out_dir]
        # Closing releases the lock; the file stays where it is.
        os.close(handle)


def _busy(out_dir: Path, device: str, operation: str) -> BuildDirectoryBusy:
    holder = holder_of(out_dir)
    running = OPERATIONS.get(holder.get("operation", ""))
    who = holder.get("device") or device
    what = running.format(device=who) if running and who else "Another MCUHome run is working"
    since = f", started {holder['started']}" if holder.get("started") else ""
    which = f" (process {holder['pid']}{since})" if holder.get("pid") else ""
    escape = "\n    mcuhome device build <device> --build-dir <dir>" if operation == "build" else ""
    return BuildDirectoryBusy(
        f"{what} in {out_dir}{which}.",
        hint=(
            "wait for it to finish — two runs in one build directory overwrite each "
            "other's files half-way through."
            + (
                " To build in parallel anyway, give this one a directory of its own:"
                if escape
                else ""
            )
            + escape
        ),
    )
