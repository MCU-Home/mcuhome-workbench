# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Starting a program, reading its log, and deciding when it has had enough.

The host-side machinery every build profile needs and none of them owns.
A build environment is entered either as a child process on this machine
(:mod:`mcuhome.workbench.subprocessbuild`) or as a container
(:mod:`mcuhome.workbench.containerbuild`), and both do the same three
things with the process they end up holding: stream its merged output
while it runs, keep it addressable so a caller can stop it, and walk a
ladder when it will not stop.

**Merged output, on purpose.** Standard output and standard error of a
build are one stream: a build log with the two halves interleaved by
anybody but the build is a log nobody can read.

**Two handles, and the difference between them is the point.**
:class:`_Child` is a process; the absent one answers every question with
``None``, which reads as "still running" to anything that only calls
``poll()``. :attr:`Running.started` is what tells the two apart, and a
caller that skips it turns "this program does not exist" into a wait
that ends at the deadline.

Nothing here knows what a build is, what a container is, or what the
program it started does.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "Completed",
    "LineSink",
    "Liveness",
    "Runner",
    "Running",
    "Spawner",
    "current_user",
    "run_command",
    "spawn_process",
]

logger = logging.getLogger(__name__)

#: Where a command's merged output goes, line by line, as it arrives.
LineSink = Callable[[str], None]


@dataclass(frozen=True)
class Completed:
    """One finished command: exit status and merged output.

    ``status`` is ``None`` when the program does not exist at all, the one
    failure that has to be told apart from a non-zero exit — "docker is
    not installed" and "docker said no" have different fixes.
    """

    status: int | None
    output: str

    @property
    def ok(self) -> bool:
        return self.status == 0


#: The one impure operation, injectable so the suite never needs a
#: container runtime. A default bound in a signature cannot be replaced
#: by monkeypatching the module, and a test that thinks it stubbed the
#: runtime out but did not is a test that starts a real build — so it is
#: resolved at call time.
Runner = Callable[[Sequence[str], "LineSink | None"], Completed]


def current_user() -> str | None:
    """``uid:gid`` of whoever is asking, where that is a thing.

    Everything a containerized build writes lands on a bind mount this
    process reads back — ``out`` above all — so the container runs as the
    calling user and leaves nothing owned by root behind.
    """
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:  # pragma: no cover - not POSIX
        return None
    return f"{getuid()}:{getgid()}"


def run_command(argv: Sequence[str], on_line: LineSink | None = None) -> Completed:
    """Run *argv*, streaming to *on_line* when given, else capturing.

    The streaming branch is for a command whose output is worth watching
    while it happens — fetching a container image, say; every other
    command is short and wants its output as a value.
    """
    try:
        if on_line is None:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                list(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            return Completed(
                status=completed.returncode, output=completed.stdout.decode("utf-8", "replace")
            )
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError:
        return Completed(status=None, output="")
    lines: list[str] = []
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        lines.append(line)
        on_line(line)
    process.wait()
    return Completed(status=process.returncode, output="\n".join(lines))


#: How often the supervisor looks at the world while a step runs: the
#: cancel sentinel and the deadline. Half a second is short enough that a
#: cancelled build feels cancelled and long enough that a poll costs
#: nothing next to a compile.
_POLL_SECONDS = 0.5

#: How long a terminated step has before it is killed. Not configurable:
#: it is not a policy but the width of the window between "the signal was
#: delivered" and "it was ignored".
_KILL_AFTER_SECONDS = 10.0

#: How long a **killed** one has before the supervisor stops waiting. A
#: process that survives SIGKILL is not one this process can reach — it
#: is a stuck kernel state or a client whose parent is gone — and waiting
#: on it forever would trade a stuck build for a stuck orchestrator,
#: which is worse: this side is what tears the environment down, and
#: tearing it down is what actually stops the build.
_GIVE_UP_AFTER_SECONDS = 30.0


class Running(Protocol):
    """A started step, still addressable while it runs."""

    def poll(self) -> int | None:
        """Its exit status, or ``None`` while it is still running."""

    def wait(self) -> int | None:
        """Block until it ends and answer its exit status."""

    def terminate(self) -> None:
        """SIGTERM. Signalling something that already exited is not news."""

    def kill(self) -> None:
        """SIGKILL. Same."""


#: How a step is started: composed argv and a log sink in, a handle out.
#: Separate from :data:`Runner` because a step is the one command here
#: that is neither short nor bounded — its output is the build log and
#: has to arrive while the build runs, and it has to stay addressable so
#: that liveness policy can reach it.
Spawner = Callable[[Sequence[str], "LineSink | None"], Running]


class _Child:
    """A real child process, with its log pumped by a thread.

    The pump is a thread rather than the calling loop because the caller
    has a second job while the build runs: watching the cancel sentinel
    and the deadline. Reading a pipe to its end and watching a clock
    cannot both be the thing a single thread is blocked on.
    """

    #: This handle is a process. The counterpart on :class:`_Absent` is
    #: what lets a caller tell "it ran and has not finished" from "it
    #: never started at all" — two states ``poll()`` cannot distinguish,
    #: and the difference between waiting for a build and waiting for
    #: nothing.
    started = True

    def __init__(self, process: subprocess.Popen[bytes], on_line: LineSink | None) -> None:
        self._process = process
        self._lines: list[str] = []
        self._pump = threading.Thread(
            target=self._drain, args=(on_line,), name="mcuhome-build-log", daemon=True
        )
        self._pump.start()

    def _drain(self, on_line: LineSink | None) -> None:
        stream = self._process.stdout
        if stream is None:
            return
        for raw in stream:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            self._lines.append(line)
            if on_line is not None:
                on_line(line)

    @property
    def output(self) -> str:
        return "\n".join(self._lines)

    def poll(self) -> int | None:
        return self._process.poll()

    def wait(self) -> int | None:
        status = self._process.wait()
        # Joined so that the last lines of a build are delivered before
        # its status is: a caller that renders the log and then the
        # verdict must not get them the other way round.
        self._pump.join(timeout=_POLL_SECONDS * 4)
        return status

    def terminate(self) -> None:
        with contextlib.suppress(OSError):
            self._process.terminate()

    def kill(self) -> None:
        with contextlib.suppress(OSError):
            self._process.kill()


def spawn_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    on_line: LineSink | None = None,
) -> Running:
    """Start *argv* and hand back a handle that streams its merged output.

    *env* and *cwd* are stated rather than inherited, which matters for
    the caller this exists for: the subprocess profile of the build
    environment specification runs the builder as a child of this
    process, and a child that inherited this process's environment would
    build differently depending on the shell it was started from. A
    ``None`` env inherits, which is what the container profile wants —
    there the environment that matters is the container's, and this
    process only starts the runtime's client.
    """
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=None if env is None else dict(env),
            cwd=None if cwd is None else str(cwd),
        )
    except OSError:
        return _Absent()
    return _Child(process, on_line)


def _spawn_command(argv: Sequence[str], on_line: LineSink | None = None) -> Running:
    """:func:`spawn_process` in the shape :data:`Spawner` has.

    The seam a container runtime uses by default, and the name a caller
    replaces to drive a build without one.
    """
    return spawn_process(argv, on_line=on_line)


class _Absent:
    """No program at all: the one failure a spawn has of its own.

    ``poll()`` and ``wait()`` answer ``None`` because there is no status
    to report, which reads as "still running" to anything that only asks
    them — so :attr:`started` is what a caller checks when the difference
    matters. It matters wherever a supervisor waits: a deadline is the
    wrong way to find out that a program does not exist.
    """

    started = False
    output = ""

    def poll(self) -> int | None:
        return None

    def wait(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


@dataclass(frozen=True)
class Liveness:
    """A sentinel, a deadline, and the hard path.

    The ladder, in order and with the reason for each rung:

    1. **The cancel sentinel.** Its *existence* means stop. It is the
       orchestrator's own file: build-environment specification
       generation 3 defines no cooperative cancellation, so nothing
       inside the environment ever sees it, and it is only how the
       decision to signal reaches this loop.
    2. **SIGTERM at** :attr:`cancel_grace_seconds`, to the process this
       side started. In the subprocess profile that is the builder
       itself; in the container profile it is the runtime's client, and
       what actually stops the build there is removing the container.
    3. **SIGKILL**, ten seconds later, for a process that ignored it.

    The deadline enters at the top of the same ladder rather than beside
    it, so that a step which runs too long walks exactly the rungs a
    cancelled one does.
    """

    cancel: Path
    deadline_seconds: int
    cancel_grace_seconds: int

    def supervise(self, child: Running, *, on_poll: Callable[[], None] | None = None) -> int | None:
        """Wait for *child*, walking the ladder, and answer its status.

        *on_poll* is called on every tick, for a caller with something of
        its own to watch on the same clock — a second clock would be a
        second thing to get wrong.
        """
        deadline = time.monotonic() + self.deadline_seconds
        stopping_at: float | None = None
        terminated_at: float | None = None
        killed_at: float | None = None
        while child.poll() is None:
            time.sleep(_POLL_SECONDS)
            if on_poll is not None:
                on_poll()
            now = time.monotonic()
            if stopping_at is None and self.cancel.exists():
                stopping_at = now
            if stopping_at is None and now >= deadline:
                # Suppressed because the directory may be gone already:
                # a caller may be tearing the session down around a
                # deadline that fired into the race, and the next rung
                # reaches the process either way.
                with contextlib.suppress(OSError):
                    self.cancel.touch()
                stopping_at = now
            if (
                terminated_at is None
                and stopping_at is not None
                and now >= stopping_at + self.cancel_grace_seconds
            ):
                child.terminate()
                terminated_at = now
            if terminated_at is not None and now >= terminated_at + _KILL_AFTER_SECONDS:
                child.kill()
                killed_at = killed_at or now
            if killed_at is not None and now >= killed_at + _GIVE_UP_AFTER_SECONDS:
                # The ladder has a last rung, and this is where it
                # ends. `None` is the honest status: nobody knows what
                # that process did, and a number would be an invention.
                logger.warning("gave up waiting for a step that survived SIGKILL")
                return None
        status = child.wait()
        # One last drain: a caller watching on this clock gets the state
        # the step left behind, not the one it had half a second before
        # it exited.
        if on_poll is not None:
            on_poll()
        return status
