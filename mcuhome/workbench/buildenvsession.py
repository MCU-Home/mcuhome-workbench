# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Driving a build environment of specification generation 3, step by step.

The build environment specification (``mcuhome-sdk``
``docs/spec/build-environment-specification.md``) describes a boundary
with exactly three parts: a directory tree below one environment
variable (§4), a request document the orchestrator writes and a result
document it reads back (§6), and an entry point run once per step with
no arguments. This module is the orchestrator's side of that boundary
and nothing else.

**It is deliberately profile-agnostic.** The specification has two
profiles — the environment delivered as a container image, and the same
packages unpacked into a read-only store on the host — and every rule of
§4, §6, §7 and §8 applies to both. So the layout, the two documents and
the judging live here once, and *how a step is entered* is a function
this module is handed: a :data:`Launcher` receives the prepared
:class:`Step` and answers a handle to a running process. The subprocess
profile's launcher is
:mod:`mcuhome.workbench.subprocessbuild`; a container profile's would
mount the same directories and run the same entry point in a container.

**What a session is here.** One sequence of steps that together produce
one set of artifacts (§1), running strictly one after another (§3). Two
things distinguish a step from an invocation of the retired
build-container contract, and both are the reason this module exists
beside :mod:`mcuhome.workbench.orchestrator` rather than inside it:

* **Every step gets a fresh ``mcuhome/`` tree.** ``work`` is empty at the
  start of every step, and nothing a step wrote outside ``out`` survives
  it. The container profile achieves that by throwing the container
  away; on a host, a fresh directory per step is the same guarantee, so
  each step is laid out under a base directory of its own.
* **``out`` is the session's, not the step's.** It is created empty when
  the session starts and survives every step of it (§7), and it is the
  only thing that reaches the next one. Each step's ``mcuhome/out`` is
  therefore a link to the session's one directory, in the same way the
  container profile mounts it into every container.

**What this module never does** is decide *which* environment runs, fetch
anything, or know what a build context is. It is handed a context
directory, an SDK tree and an entry point, and it drives steps against
them.

The answer is :class:`~mcuhome.workbench.orchestrator.LocalOutcome` —
the same type the legacy container invocation (retired at the switchover)
produces — so that a caller
which only wants a firmware never has to ask which profile ran.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcuhome.model.artifacts import Artifact
from mcuhome.model.hashes import sha256_file

from mcuhome.workbench.orchestrator import (
    LineSink,
    Liveness,
    LocalOutcome,
    Running,
    contained,
    write_request,
)

__all__ = [
    "ACTION_BUILD",
    "ARTIFACT_ROLES",
    "BASE_DIR_VAR",
    "CACHE_TIERS",
    "CCACHE_SUBDIR",
    "ENTRY_POINT",
    "REQUEST_FILE",
    "SPEC_GENERATION",
    "STATUS_FAILURE",
    "STATUS_SUCCESS",
    "STATUS_UNSUPPORTED",
    "STEP_DIR",
    "BuilderSession",
    "CacheTier",
    "Launcher",
    "Step",
    "judge_step",
    "step_request",
    "verify_step_artifacts",
]


# --------------------------------------------------------------------------
# The frozen names of the specification, from the orchestrator's side
# --------------------------------------------------------------------------

#: The generation this orchestrator speaks. It goes into every request
#: document, and an environment that implements another one answers
#: ``unsupported`` rather than guessing (§12).
SPEC_GENERATION = 3

#: The one environment variable the specification defines (§4). Every
#: path of a step is resolved against it, by both sides, and none of them
#: is kept from one step to the next.
BASE_DIR_VAR = "MCUHOME_BUILDER_BASE_DIR"

#: §4's tree below the base directory. Everything in here is the
#: orchestrator's; everything outside it is the environment's own content.
STEP_DIR = "mcuhome"
REQUEST_FILE = "invocation-request.json"
STEP_WORK = "work"
STEP_OUT = "out"
STEP_SDK = "sdk"
STEP_CONTEXT = "build-context"
STEP_CACHE = "cache"
STEP_BIN = "bin"

#: The entry point, at the path §4 fixes, relative to :data:`STEP_DIR`.
#: Run with **no arguments**; everything the step is about is in the
#: request document.
ENTRY_POINT = "build-environment-entry"

#: §8's four cache tiers, most local first. ``local`` is the only one
#: that is always present and always writable; the others exist when the
#: orchestrator provides them and are absent otherwise, which the
#: specification explicitly allows.
CACHE_TIERS = ("local", "session", "project", "shared")

#: Where a cache tier keeps the compiler cache (§8: "**ccache** goes in
#: ``<tier>/ccache``"). Stated here because the orchestrator creates the
#: directory in the tier it made writable, so that an environment finds a
#: cache rather than a missing path.
CCACHE_SUBDIR = "ccache"

#: The result document of one step (§6.2). ``result-*.json`` at the top
#: of ``out`` is reserved for it.
RESULT_PREFIX = "result-"
RESULT_SUFFIX = ".json"

#: §6.2's three statuses. ``unsupported`` is the one that means *no
#: environment of my kind can do this*, which is a fact about the
#: environment rather than about the build.
STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_UNSUPPORTED = "unsupported"
_STATUSES = (STATUS_SUCCESS, STATUS_FAILURE, STATUS_UNSUPPORTED)

#: The one action MCUHome's own environments implement today
#: (``mcuhome-sdk`` ``docs/spec/build-actions.md``). Which actions exist
#: is the orchestrator's vocabulary and not the specification's, which is
#: why the name is stated here rather than derived from anything.
ACTION_BUILD = "build"

#: What the orchestrator knows the fixed artifact names of the ``build``
#: action are for. A result document lists artifacts by name and nothing
#: else, and the names are fixed by the action's own documentation — so
#: this is the whole of the mapping from a delivered file to the role a
#: consumer looks for. The keys are declared paths and therefore match at
#: the top of ``out`` alone, which is where the action's table puts them:
#: a ``firmware.bin`` inside a subdirectory is a file of the
#: environment's own naming and not the image anybody signs.
#: "An environment may write more files into ``out/`` and list them; the
#: orchestrator uses the ones it knows": a name that is not in here is
#: carried with an empty role rather than dropped, because a file the
#: environment declared is worth reporting even when nothing downstream
#: asks for it by role.
ARTIFACT_ROLES = {
    "firmware.bin": "firmware",
    "firmware.hex": "firmware",
    "bootloader.hex": "bootloader",
    "build-report.json": "report",
}

#: The one ``root`` an artifact of a v3 step can have: ``out`` is the only
#: directory an environment delivers through.
ROOT_OUT = "out"

#: A session or invocation id this orchestrator is willing to put into a
#: path. §6.1 promises an ``invocation_id`` is "safe to use directly in a
#: filename", which is a promise this side has to keep rather than one it
#: may rely on: the id is drawn here.
_SAFE_ID = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_"


def _is_safe_id(value: str) -> bool:
    return bool(value) and value[0].isalnum() and all(char in _SAFE_ID for char in value)


# --------------------------------------------------------------------------
# The two documents
# --------------------------------------------------------------------------


def step_request(
    *,
    session_id: str,
    invocation_id: str,
    action: str,
    parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The request document of one step (§6.1).

    Five fields, all of them mandatory, in the order the specification
    prints them. ``parameters`` is written even when it is empty, because
    the specification's own example writes ``{}`` and an environment that
    reads ``parameters`` without checking for its absence is not wrong.

    There is deliberately nothing else in it. The legacy container
    invocation (retired at the switchover) carries the whole layout in
    its request document — every directory as an absolute
    path, the trees, the limits — because the program was told where
    things were; here §4 fixes the tree relative to one environment
    variable, so a path in this document would be a second source of
    truth for something the environment already knows.
    """
    return {
        "spec_generation": SPEC_GENERATION,
        "session_id": session_id,
        "invocation_id": invocation_id,
        "action": action,
        "parameters": dict(parameters or {}),
    }


def judge_step(
    path: Path,
    *,
    action: str,
    invocation_id: str,
    exit_code: int | None,
    context_id: str = "",
) -> LocalOutcome:
    """Read the result document if it exists, and judge it (§6.2, §6.3).

    "The orchestrator reads the result document whenever it exists,
    whatever the exit code. A step that produced no readable result
    document failed, whatever it exited with." So the document is read
    first and the exit code is one more thing to compare it against, not
    a gate in front of it.

    A step is successful exactly when the document parses as an object,
    states a generation this orchestrator speaks, echoes the invocation
    id it was asked about, says ``success``, and the process exited zero.
    Everything else is a problem, and the problems are the message a
    caller renders.

    :attr:`~mcuhome.workbench.orchestrator.LocalOutcome.status` carries
    the environment's own word, ``unsupported`` included: it means *no
    environment of my kind can do this* and tells the caller to look for
    a different environment rather than report a broken build, which is a
    decision this function must not make for it.

    :attr:`~mcuhome.workbench.orchestrator.LocalOutcome.violation` is
    §6.3's contradiction — a document that says ``success`` after a
    non-zero exit, or a zero exit after anything else. It fails the step
    either way; carrying it separately is what lets a caller say that the
    *environment* misbehaved rather than the build.
    """
    outcome = LocalOutcome(action=action, context_id=context_id, exit_code=exit_code, result=None)
    data = _load(path)
    if data is None:
        outcome.problems = (
            f"the build environment wrote no readable result document at {path.name}",
        )
        outcome.status = STATUS_FAILURE
        if exit_code == 0:
            # §6.3 from the other side: exiting zero is a claim that a
            # success document was written, and there is none.
            outcome.violation = "the build environment exited 0 and wrote no result document"
        return outcome

    outcome.result = data
    problems: list[str] = []
    status = data.get("status")
    outcome.status = status if status in _STATUSES else STATUS_FAILURE

    generation = data.get("spec_generation")
    if generation != SPEC_GENERATION:
        problems.append(
            f"the result document states specification generation {generation!r} and this "
            f"orchestrator speaks {SPEC_GENERATION}"
        )
    if data.get("invocation_id") != invocation_id:
        problems.append(
            f"the result document echoes invocation {data.get('invocation_id')!r} for a step "
            f"this orchestrator called {invocation_id!r}"
        )
    if status not in _STATUSES:
        problems.append(f"the result document states status {status!r}")
    elif status != STATUS_SUCCESS:
        message = data.get("message")
        said = f": {message}" if isinstance(message, str) and message.strip() else ""
        problems.append(f"the build environment reported status {status!r}{said}")
    if exit_code != 0:
        problems.append(f"the build environment exited {exit_code}")

    outcome.violation = _violation(status, exit_code)
    outcome.problems = tuple(problems)
    outcome.successful = not problems
    return outcome


def _violation(status: object, exit_code: int | None) -> str | None:
    """§6.3, as a sentence about the environment rather than the build.

    "Exit ``0`` when you wrote a result document with ``status:
    "success"``, and non-zero otherwise." Both directions are a
    contradiction the environment produced, and neither can be repaired
    by looking at the other side harder.
    """
    successful = status == STATUS_SUCCESS
    if successful and exit_code != 0:
        return (
            f"the result document says {STATUS_SUCCESS!r} and the build environment "
            f"exited {exit_code}"
        )
    if not successful and exit_code == 0:
        return f"the build environment exited 0 and its result document says {status!r}"
    return None


def _load(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


# --------------------------------------------------------------------------
# Egress: what the step says it delivered, checked against what is there
# --------------------------------------------------------------------------


def verify_step_artifacts(
    out: Path, declared: Sequence[Any]
) -> tuple[tuple[Artifact, ...], tuple[str, ...]]:
    """The declared artifacts, resolved under ``out`` and hashed (§6.2, §7).

    §6.2 declares artifacts as "the files **this step** wrote into
    ``out/``, as paths relative to ``out/``" — names, and no hashes: an
    environment states what it produced and the orchestrator measures the
    bytes, which is the only order in which the measurement is worth
    anything.

    The walk under ``out`` is
    :func:`mcuhome.workbench.orchestrator.contained` — the same one the
    legacy container invocation (retired at the switchover) uses for its
    egress, because an egress check that exists
    twice is an egress check that will differ once.

    §7 is what is enforced on the way: "Put only regular files and
    directories in ``out``. No symlinks, hard links, device nodes,
    sockets or FIFOs — the orchestrator rejects them, because these files
    travel to other people's machines." So the path is walked segment by
    segment with ``lstat`` and never :meth:`Path.resolve`: what matters is
    that no segment is a link at all, because a link that *points* inside
    ``out`` resolves to a contained-looking path and would then be served,
    with its target's bytes, under the declared name.

    Returns ``(verified, problems)``. A declaration that is not a usable
    relative path, or that names something absent or unservable, is a
    problem: the environment said it delivered a file and it did not.
    """
    verified: list[Artifact] = []
    problems: list[str] = []
    for entry in declared:
        if not isinstance(entry, str) or not entry:
            problems.append(
                f"the result document declares an artifact that is not a name: {entry!r}"
            )
            continue
        resolved = contained(out, entry)
        if resolved is None:
            problems.append(
                f'the declared artifact "{entry}" is not contained in out: a segment is '
                "absent, a symlink, or leaves the directory"
            )
            continue
        info = _lstat(resolved)
        if info is None:
            problems.append(f'the declared artifact "{entry}" is not present under out')
            continue
        if not stat.S_ISREG(info.st_mode):
            problems.append(
                f'the declared artifact "{entry}" is not a regular file: a symlink, device '
                "node, FIFO or socket is not a servable artifact"
            )
            continue
        if info.st_nlink > 1:
            problems.append(
                f'the declared artifact "{entry}" is a hardlink (nlink {info.st_nlink}): '
                "a second name for bytes that may live outside out"
            )
            continue
        verified.append(
            Artifact(
                root=ROOT_OUT,
                path=entry,
                role=ARTIFACT_ROLES.get(entry, ""),
                sha256=sha256_file(resolved),
            )
        )
    return tuple(verified), tuple(problems)


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


# --------------------------------------------------------------------------
# The tree of one step, and the session it belongs to
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheTier:
    """One cache tier the orchestrator provides, and whether it may be written.

    §8 gives a step four tiers and says what an environment may assume
    about each: ``local`` is always writable, everything else is
    read-only unless the orchestrator says otherwise and may be missing
    entirely. A tier this orchestrator was given no directory for is
    simply not created — "may be missing entirely" is the specification's
    own wording, and an empty directory would be a warm cache that is not
    one.

    Read-only is **not enforced** here: a tier is a directory the
    environment sees, and in the subprocess profile there is no mount to
    make read-only with. The flag is what decides where this orchestrator
    points a compiler cache, and a tier it does not own it does not point
    anything at.
    """

    path: Path
    writable: bool = False


#: How a step is entered. The prepared :class:`Step` and a log sink in, a
#: handle to the running entry point out — the one thing that differs
#: between the specification's two profiles, and therefore the one thing
#: this module does not implement.
Launcher = Callable[["Step", "LineSink | None"], Running]


@dataclass(frozen=True)
class Step:
    """One prepared step: its tree on disk, and the way to run it.

    Everything here exists **before** the entry point is started, which
    is the point of the class: a caller that may want to stop the step
    needs :meth:`stop` while it runs, and a launcher needs the layout to
    compose an environment from.
    """

    session: BuilderSession
    #: Opaque to the environment, and the name its result document is
    #: called after (§6.1: "safe to use directly in a filename").
    invocation_id: str
    #: What this step was asked to do — the request document's ``action``.
    action: str
    #: What ``MCUHOME_BUILDER_BASE_DIR`` is set to for this step.
    base_dir: Path
    #: §4's tree, resolved: the directories a launcher and a caller need.
    request: Path
    work: Path
    out: Path
    entry_point: Path
    #: The stop sentinel. It is the *orchestrator's* file and is never
    #: named in the request document: generation 3 defines no cooperative
    #: cancellation, so what stops a step is a signal and this is only
    #: how the decision to send one reaches the supervising loop.
    cancel: Path
    #: The tiers this step actually has, by name. ``local`` is always in
    #: here; the others only when the orchestrator provided them.
    cache: Mapping[str, Path] = field(default_factory=dict)
    #: The most local tier this orchestrator owns a writable directory in
    #: — where a compiler cache goes and survives.
    writable_cache: Path | None = None

    @property
    def result(self) -> Path:
        """Where this step's result document is (§6.2)."""
        return self.out / f"{RESULT_PREFIX}{self.invocation_id}{RESULT_SUFFIX}"

    def stop(self) -> None:
        """Ask for this step to be stopped, and never raise for asking twice.

        The environment is not told: generation 3 has no cancel sentinel
        and no ``cancelled`` status, so a step that is stopped is a step
        that failed without writing a result document. What follows is
        the signal ladder — SIGTERM, then SIGKILL — and in this profile
        the signal reaches the builder itself rather than a client in
        front of it.
        """
        with contextlib.suppress(OSError):
            self.cancel.touch()

    def run(self, *, on_line: LineSink | None = None) -> LocalOutcome:
        """Run the entry point, relay its log, and judge what came back."""
        session = self.session
        child = session.launcher(self, on_line)
        status = session.liveness(self).supervise(child)
        return self._collect(status)

    def _collect(self, exit_code: int | None) -> LocalOutcome:
        """§6.2 and §6.3 for the document, §7 for the files it declares."""
        outcome = judge_step(
            self.result,
            action=self.action,
            invocation_id=self.invocation_id,
            exit_code=exit_code,
            context_id=self.session.context_id,
        )
        outcome.out = self.out
        if outcome.result is not None:
            declared = outcome.result.get("artifacts")
            entries = declared if isinstance(declared, list) else []
            verified, problems = verify_step_artifacts(self.out, entries)
            outcome.artifacts = verified
            if not isinstance(declared, list) and declared is not None:
                problems = problems + ("the result document's artifacts are not a list",)
            if problems:
                outcome.problems = outcome.problems + problems
                outcome.successful = False
        return outcome


class BuilderSession:
    """A session (§1) against one build environment, driven step by step.

    Created with everything a step is *about* — the context directory,
    the SDK tree the context pinned, the entry point of the environment
    that runs — and with the :data:`Launcher` that knows how to enter a
    step in the profile in use. :meth:`invoke` then runs one action to
    its end and answers the same
    :class:`~mcuhome.workbench.orchestrator.LocalOutcome` every other
    build path answers.

    **The session owns ``out``** and nothing else that survives a step.
    It is created empty here and every step's ``mcuhome/out`` points at
    it, which is §7's "created empty when the session starts and survives
    every step of it" implemented the only way a host can implement it.

    **Steps are strictly sequential** (§3). A second one cannot be
    prepared while one is running, and a step whose base directory the
    next one replaces is exactly what "nothing you write survives a step"
    means: the previous step's tree is removed when the next is prepared,
    so a session's disk footprint is one build tree and not one per step.
    The last one is kept, because it is the one somebody looks into when
    a build failed.
    """

    def __init__(
        self,
        *,
        root: Path,
        context_dir: Path,
        sdk_tree: Path,
        entry_point: Path | None,
        launcher: Launcher,
        context_id: str = "",
        session_id: str | None = None,
        tiers: Mapping[str, CacheTier] | None = None,
        deadline_seconds: int = 5400,
        cancel_grace_seconds: int = 0,
    ) -> None:
        self.root = Path(root).resolve()
        self.context_dir = Path(context_dir).resolve()
        self.sdk_tree = Path(sdk_tree).resolve()
        #: The entry point to place at §4's path, or ``None`` for a
        #: profile whose delivery already puts one there — an image
        #: carries its own, and linking over it would replace the
        #: environment's content with the orchestrator's idea of it.
        self.entry_point = Path(entry_point).resolve() if entry_point is not None else None
        self.launcher = launcher
        self.context_id = context_id
        self.session_id = session_id or uuid.uuid4().hex[:12]
        if not _is_safe_id(self.session_id):
            raise ValueError(
                f"a session id has to be usable in a file name: {self.session_id!r} is not"
            )
        self.tiers = dict(tiers or {})
        self.deadline_seconds = deadline_seconds
        self.cancel_grace_seconds = cancel_grace_seconds
        self._counter = 0
        self._current: Step | None = None
        self._running = False
        self._closed = False

        # Every one of them **fresh**, and that is not tidiness. A
        # caller's session root is an ordinary path it reuses — the same
        # build directory builds the same device again — so an `out` that
        # was merely created-if-absent would start the session holding the
        # last one's artifacts, which §7 forbids ("created empty when the
        # session starts") and which would let a stale `firmware.bin`
        # travel out of a build that never wrote one. `steps` is the same
        # question with disk attached: one build tree per build, kept
        # forever. The legacy container invocation (retired at the
        # switchover) clears its own for exactly these two reasons.
        self.out = _fresh(self.root / "out")
        self._steps = _fresh(self.root / "steps")
        self._control = _fresh(self.root / "control")
        #: A home directory for a builder whose caller has none to state.
        #: Outside every step's base directory on purpose: §4 says
        #: everything outside ``mcuhome/`` is the environment's own
        #: content, and this is the orchestrator's.
        self.home_dir = _fresh(self.root / "home")

    def __enter__(self) -> BuilderSession:
        return self

    def __exit__(self, *_exception: object) -> None:
        self.close()

    def liveness(self, step: Step) -> Liveness:
        """The supervision policy for *step*.

        The same ladder the container path walks, with the top rung
        missing: generation 3 defines no cancel sentinel the environment
        could see, so the grace period between "stop" and SIGTERM buys
        nothing here and is zero by default. What the ladder still does
        is enforce the deadline and make sure a stopped step really ends.
        """
        return Liveness(
            cancel=step.cancel,
            deadline_seconds=self.deadline_seconds,
            cancel_grace_seconds=self.cancel_grace_seconds,
        )

    def prepare(self, action: str, *, parameters: Mapping[str, Any] | None = None) -> Step:
        """Lay out one step's tree and write its request document.

        Everything §4 lists, in a directory of this step's own:
        ``work`` empty, ``out`` and ``sdk`` and ``build-context`` pointing
        at the session's, the cache tiers the orchestrator provides, the
        entry point at the path the specification fixes, and the request
        document last — because it is the file whose presence means the
        step may start.
        """
        if self._closed:
            raise RuntimeError("this build environment session has been closed")
        if self._running:
            raise RuntimeError(
                "steps of a session run strictly one after another; one is still running"
            )
        self._counter += 1
        invocation_id = f"{self.session_id}-{self._counter}"
        # The previous step's tree is dead by definition — nothing it
        # wrote outside `out` survives it — and keeping it would keep a
        # whole build tree per step.
        if self._current is not None:
            shutil.rmtree(self._current.base_dir, ignore_errors=True)

        base = self._steps / invocation_id
        mcuhome = base / STEP_DIR
        work = mcuhome / STEP_WORK
        work.mkdir(mode=0o700, parents=True)
        _link(mcuhome / STEP_OUT, self.out)
        _link(mcuhome / STEP_SDK, self.sdk_tree)
        _link(mcuhome / STEP_CONTEXT, self.context_dir)
        cache, writable = self._lay_out_cache(mcuhome / STEP_CACHE)
        entry_point = mcuhome / STEP_BIN / ENTRY_POINT
        if self.entry_point is not None:
            entry_point.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _link(entry_point, self.entry_point)

        step = Step(
            session=self,
            invocation_id=invocation_id,
            action=action,
            base_dir=base,
            request=mcuhome / REQUEST_FILE,
            work=work,
            out=self.out,
            entry_point=entry_point,
            cache=cache,
            writable_cache=writable,
            cancel=self._control / f"{invocation_id}.cancel",
        )
        write_request(
            step_request(
                session_id=self.session_id,
                invocation_id=invocation_id,
                action=action,
                parameters=parameters,
            ),
            step.request,
        )
        self._current = step
        return step

    def invoke(
        self,
        action: str,
        *,
        parameters: Mapping[str, Any] | None = None,
        on_line: LineSink | None = None,
    ) -> LocalOutcome:
        """Prepare one step, run it, and judge it."""
        step = self.prepare(action, parameters=parameters)
        self._running = True
        try:
            return step.run(on_line=on_line)
        finally:
            self._running = False

    def close(self) -> None:
        """End the session. Idempotent, and never the reason a build fails.

        Nothing is torn down: a subprocess profile's step is over when
        its process is, and the session's directories are the caller's —
        it reads the artifacts out of ``out`` after this returns.
        """
        self._closed = True

    def _lay_out_cache(self, cache: Path) -> tuple[dict[str, Path], Path | None]:
        """§8's tiers for one step, and where a compiler cache may go.

        ``local`` is created when nobody provided a directory for it: the
        specification promises it is always there and always writable,
        and the environment is told it survives nothing — so a fresh
        directory per step is the literal reading. An orchestrator that
        *does* provide one is making the environment's primary cache
        durable, which §8 explicitly allows ("the orchestrator may or may
        not mount something over it") and which is the only way this
        profile has a compiler cache at all: MCUHome's own environment
        points ccache at the most local tier it can write.

        Every other tier exists exactly when a directory was provided for
        it. A tier that is absent is not an error and not an empty
        directory — an environment reads "may be missing entirely" and
        does the right thing, while an empty directory would look like a
        cold cache that is actually a missing one.
        """
        cache.mkdir(mode=0o700, parents=True)
        present: dict[str, Path] = {}
        writable: Path | None = None
        for name in CACHE_TIERS:
            tier = self.tiers.get(name)
            target = cache / name
            if tier is None:
                if name == "local":
                    target.mkdir(mode=0o700)
                    present[name] = target
                    writable = writable or target
                continue
            tier.path.mkdir(parents=True, exist_ok=True)
            _link(target, tier.path)
            present[name] = tier.path
            if tier.writable and writable is None:
                writable = tier.path
        return present, writable


def _fresh(directory: Path) -> Path:
    """*directory*, existing and empty, whatever was there before.

    ``ignore_errors`` because what is being removed is this
    orchestrator's own scratch state: a leftover it cannot delete is
    worth neither a failed build nor a repair, and everything that
    matters is created after it. Removal never follows a link — the
    directories being cleared are full of them, and they point at the
    session's own ``out``, at the SDK and at the caller's build context.
    """
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def _link(link: Path, target: Path) -> None:
    """Point *link* at *target*, absolutely.

    The subprocess profile's answer to what the container profile does
    with a bind mount: the session's ``out``, the SDK it was handed and
    the build context appear inside every step's tree without being
    copied into it. Absolute, because a step's tree is thrown away and
    recreated at a different name while the things it points at stay
    where they are.
    """
    link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    link.symlink_to(target)
