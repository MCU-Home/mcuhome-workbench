# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``local`` build method: driving a build container through the ABI.

This is the E32 resolution and the E51 decision, in one module. Until
now a local build (:mod:`mcuhome.compiler.container`) assembled a ``west
build`` command **on the host** and wrapped it in a one-shot ``docker
run`` that bind-mounted the host's own west workspace. That is the
violation ADR 0007's own contract calls out: the image is the build
environment, and a backend that hands it a host-assembled command is
generating MCUHome firmware outside the one path the contract defines.
The ``local`` method here does instead what the build-container contract
§5 makes every backend do — it writes the request document, execs
``/mcuhome/run <action> <request>`` in a container, reads the result
document back and judges it against §5.3 — with **no server, no auth, no
sockets, no sessions**. It plays the contract's backend role directly.

**Why the ABI is now driven twice** (E51). The build server
(:mod:`mcuhome_buildserver`) drives the same frozen ABI, and this module
does **not** import it: the build server is the fat, networked half of
the product and the ``local`` method is the thin, in-process half, and
ADR 0020 decision 4 keeps the import edge from the thin one to the fat
one from ever existing. The two are the same lifecycle written for two
worlds — a session state machine over a socket there, a single blocking
drive here — so the structure is mirrored piece by piece and the code is
this package's own. Reading the server's backend as a reference is how
this stays faithful; importing it is what ADR 0020 forbids.

**Synchronous, on purpose.** A ``local`` build is one context, one
container, one invocation, driven start to finish by the caller that
asked for it — there is no second client to keep a socket warm for and no
build to leave running detached. So this backend blocks on the ``docker
exec`` and streams the log to a sink as it comes. The async commitment of
:mod:`mcuhome.workbench.api` is the *session client*'s (ADR 0020 decision
6, the remote method), which drives a socket and genuinely waits; this
one drives a subprocess and is done when it returns.

**The same-path principle.** Every mount is host path to the identical
container path, so the request document's absolute paths mean the same
thing on both sides of the boundary — which is what makes a stalled build
inspectable from the host and, since the session tree is mounted piece by
piece rather than wholesale, what makes the request document's paths
*exactly* the set the container can see. The one mount that is not
path-identical is the SDK when ``describe`` declares a fixed path for it:
the tree is unpacked at a backend-chosen path and mounted at the path the
image requires (§4.1).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import zstandard

from mcuhome.compiler import abi

# `Artifact` is vocabulary, not backend machinery: the `remote` method
# reports the same four fields off the session protocol's verdict and may
# not import this module to say so (ADR 0020 decision 3), so the class
# lives in `mcuhome-model` and is re-exported here — `localbackend.Artifact`
# stays the name every caller already uses.
from mcuhome.model.artifacts import Artifact
from mcuhome.model.context import ContextManifest
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file
from mcuhome.model.toolchain import satisfies_line
from mcuhome.workbench.contextdir import read_context_manifest

# The package name, the index file name and the resolution rule are the
# client's as much as this backend's — the same directory is read by
# whoever resolves a pin and by whoever fetches the bytes — so all three
# live in the workbench and are re-exported here under the names this
# module already offered (`localbackend.SDK_PACKAGE_NAME`).
from mcuhome.workbench.resolve_pins import INDEX_FILE, SDK_PACKAGE_NAME, resolve_from_index

__all__ = [
    "ACTION_BUILD",
    "ACTION_DESCRIBE",
    "ACTION_VERIFY",
    "DESCRIBE_FILE",
    "IDLE_COMMAND",
    "PROGRAM",
    "SDK_PACKAGE_NAME",
    "Artifact",
    "BackendConfig",
    "Completed",
    "Docker",
    "ImageProfile",
    "LineSink",
    "LocalBackend",
    "LocalOutcome",
    "Mount",
    "ResourceLimits",
    "SdkPackage",
    "TreeEntry",
    "acquire_sdk",
    "current_user",
    "describe_run_command",
    "exec_command",
    "inspect_command",
    "judge_result",
    "read_file_command",
    "remove_command",
    "request_document",
    "start_command",
    "verify_artifacts",
    "write_request",
]

# --------------------------------------------------------------------------
# The frozen names of the contract, from the backend's side
# --------------------------------------------------------------------------

#: The program every conforming image carries, at the one absolute path
#: §2.2 fixes. Never looked up on ``PATH``: the invocation is resolved
#: without a shell, ``docker exec`` inherits the environment fixed at
#: container creation, and ``PATH`` inside the image is the image
#: author's — so a bare name would be a promise about someone else's
#: filesystem.
PROGRAM = "/mcuhome/run"

#: The optional static self-description an image MAY carry (§2.2.1). Read
#: with ``docker run --rm … cat`` before a container is arranged, because
#: §6.1 splits MCUHome's own program in two — the launcher is image
#: content, the body arrives with ``trees.sdk`` — so the ``program``
#: block is otherwise unobtainable until the SDK mount point is known,
#: which is exactly what the block would have told the backend.
DESCRIBE_FILE = "/mcuhome/describe.json"

ACTION_DESCRIBE = abi.IMPLEMENTED_ACTIONS[0]
ACTION_VERIFY = abi.IMPLEMENTED_ACTIONS[1]
ACTION_BUILD = abi.IMPLEMENTED_ACTIONS[2]

#: The request format version this backend writes and the result format
#: version it reads. Taken from the program half of the ABI in this same
#: package so the two never drift: the launcher this backend execs *is*
#: :mod:`mcuhome.compiler.abi`.
REQUEST_VERSION = abi.REQUEST_VERSIONS[0]
RESULT_VERSION = abi.RESULT_VERSION
CONTRACT_VERSION = abi.CONTRACT_VERSION

#: What the session's container runs as its main process. §2.2 makes
#: starting the container the backend's business — ``docker run``
#: overrides both ``ENTRYPOINT`` and ``CMD``, and the image "MUST provide
#: a POSIX shell at ``/bin/sh``" so there is always a command to name.
#: Deliberately POSIX rather than ``sleep infinity``: the contract
#: promises a shell, not GNU coreutils.
IDLE_COMMAND = ("/bin/sh", "-c", "while :; do sleep 86400; done")

#: The one legal ``artifacts[].root`` value in v1 (§5.4). "A consumer
#: that sees a ``root`` it does not know MUST skip that artifact and MUST
#: NOT resolve it against ``out``."
ROOT_OUT = "out"

#: ``artifacts[].path`` segments, §5.4 / §9.2. The same shape §9.2
#: forbids the program to leave ``out``, which is what makes egress a
#: check rather than a repair.
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._-]+\Z")

#: A bare hash in the one legal spelling of §3.3.1 — 64 lowercase hex
#: digits, no prefix. "A declared hash in any other rendering is a
#: mismatch, not a value to fold."
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")

#: A container id as docker writes it back — 64 hex digits, of which the
#: first twelve are the short form. Checked because every later command
#: puts this string in an argv.
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")

#: Every field §7.1.1 makes mandatory inside a ``program`` block, except
#: ``trees``, which is mandatory only in a ``describe`` result.
PROGRAM_FIELDS = ("id", "version", "contract", "request", "result", "actions")

#: The three ``org.mcuhome.*`` image labels §2.1 gives a meaning. Only the
#: first has a counterpart in the ``program`` block; the other two are the
#: coupling labels §2.1.1 writes a compatibility constraint over, so what
#: is checked about them is only that they are **present** — "absence is
#: never read as compatible". Spelled here rather than imported: this
#: package must not reach into the build server (ADR 0020 decision 4), and
#: the label strings are the contract's, not either implementation's.
CONTRACT_LABEL = "org.mcuhome.contract"
ZEPHYR_LABEL = "org.mcuhome.zephyr"
TOOLCHAIN_LABEL = "org.mcuhome.toolchain"

#: A generous bound on what the SDK archive unpacks to. Not a policy
#: anyone tunes — an operator who does not trust an SDK source should not
#: list it — but a corrupt or malicious archive comes out here as a
#: bounded read rather than an out-of-memory kill.
SDK_MAX_BYTES = 2 * 1024 * 1024 * 1024

#: How large a chunk the decompressor hands over at a time. Bounded
#: because a zstd frame can expand without limit, and a cap that only
#: fires after the expansion is not a cap.
_BLOCK = 1 << 20


def current_user() -> str | None:
    """``uid:gid`` of whoever is asking, where that is a thing.

    Everything the program writes lands on a bind mount this backend
    reads back — ``out``, ``work``, the result document — so the
    container runs as the calling user and leaves nothing owned by root
    behind (§9.1, the same reason the container path already runs as the
    caller).
    """
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:  # pragma: no cover - not POSIX
        return None
    return f"{getuid()}:{getgid()}"


# --------------------------------------------------------------------------
# Small value types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Mount:
    """One bind mount, host source to container destination.

    ``read_only`` is the whole of the mode. §9.1 requires the backend to
    write-protect ``context`` and every non-``writable`` tree "with the
    strongest means its profile has", and for a container that means a
    read-only bind mount — kernel-enforced, not a promise the program is
    asked to keep.
    """

    source: Path
    target: Path
    read_only: bool = False

    def to_argument(self) -> str:
        suffix = ":ro" if self.read_only else ""
        return f"{self.source}:{self.target}{suffix}"


@dataclass(frozen=True)
class ResourceLimits:
    """What the container may consume, as ``docker run`` flags.

    §1.2 lists per-session resource limits among the ``container``
    profile's guarantees and §9.1 makes them the backend's "to set and to
    enforce". They go on the ``run`` that creates the container, because a
    limit applied anywhere else is a limit a build can step around.
    """

    memory: str | None = None
    cpus: str | None = None
    pids: int | None = None

    def to_arguments(self) -> list[str]:
        argv: list[str] = []
        if self.memory:
            argv += ["--memory", self.memory]
        if self.cpus:
            argv += ["--cpus", self.cpus]
        if self.pids is not None:
            argv += ["--pids-limit", str(self.pids)]
        return argv


@dataclass(frozen=True)
class TreeEntry:
    """One ``trees`` entry: where a layer's source tree is, and its mode.

    ``writable`` is **asserted by the backend and never probed by the
    program** (§4.1), so the flag has to be truthful.
    """

    path: Path
    writable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "writable": self.writable}


@dataclass(frozen=True)
class Completed:
    """One finished docker command: exit status and merged output.

    ``status`` is ``None`` when the program does not exist at all, the one
    failure that has to be told apart from a non-zero exit — "docker is
    not installed" and "docker said no" have different fixes.
    """

    status: int | None
    output: str

    @property
    def ok(self) -> bool:
        return self.status == 0


@dataclass(frozen=True)
class SdkPackage:
    """One SDK package, found and verified. The tree is already unpacked."""

    version: str
    sha256: str
    #: Where the archive was found, for the log and for a bug report.
    source: Path
    #: The unpacked tree, which is what ``trees.sdk`` names.
    tree: Path


@dataclass(frozen=True)
class ImageProfile:
    """One image, as ``describe`` (or ``describe.json``) answers for it.

    ``program`` is the block verbatim; the accessors read exactly the
    fields a backend is entitled to act on. Cached nowhere — a ``local``
    build resolves one image once and is done.
    """

    reference: str
    digest: str | None
    labels: dict[str, str]
    program: dict[str, Any]

    @property
    def identity(self) -> str:
        found = self.program.get("id")
        return found if isinstance(found, str) else "unknown"

    @property
    def actions(self) -> tuple[str, ...]:
        found = self.program.get("actions")
        return tuple(str(name) for name in found) if isinstance(found, list) else ()

    def tree_path(self, layer: str) -> Path | None:
        """Where the image keeps *layer*, or ``None`` if it names none.

        §7.1.1: "``null`` asks, a path requires." A concrete path is where
        the image keeps that tree **and**, for a tree the backend
        supplies, the path the backend MUST supply it at; ``null`` means
        "put it wherever you like and name it in ``trees``".
        """
        trees = self.program.get("trees")
        entry = trees.get(layer) if isinstance(trees, dict) else None
        path = entry.get("path") if isinstance(entry, dict) else None
        return Path(path) if isinstance(path, str) and path.startswith("/") else None


@dataclass
class LocalOutcome:
    """What one ``local`` invocation produced, from the backend's side.

    ``successful`` is the seven-part answer of §5.3 and nothing else;
    ``violation`` is the contract violation §5.3 raises against the image
    where the exit code and the document contradict each other, carried as
    a value so a caller can log it and refuse to trust the image without
    re-deriving it.
    """

    action: str
    context_id: str
    exit_code: int | None
    result: dict[str, Any] | None = None
    status: str = "failure"
    successful: bool = False
    problems: tuple[str, ...] = ()
    violation: str | None = None
    artifacts: tuple[Artifact, ...] = field(default_factory=tuple)
    #: The invocation's ``out`` directory on the host — where every
    #: verified artifact in :attr:`artifacts` actually is (its ``path`` is
    #: relative to here). Filled in by :meth:`LocalBackend._collect` so a
    #: caller can read the delivered files back without re-deriving the
    #: per-invocation layout; ``None`` on an outcome that never reached
    #: egress (an image or SDK refusal raises before one exists).
    out: Path | None = None


@dataclass(frozen=True)
class BackendConfig:
    """Everything the ``local`` method needs that is not the context.

    ``sdk_sources`` are operator-configured local directories, searched in
    order (ADR 0019 §8, contract v1's first tier — E48). Everything else
    is the resource shape of the one container this backend starts.
    """

    sdk_sources: tuple[Path, ...]
    jobs: int
    image: str | None = None
    memory: str | None = None
    cpus: str | None = None
    pids: int | None = None
    deadline_seconds: int = 5400
    cancel_grace_seconds: int = 60

    def limits(self) -> ResourceLimits:
        return ResourceLimits(memory=self.memory, cpus=self.cpus, pids=self.pids)


# --------------------------------------------------------------------------
# The docker seam — composed argv on one side, one subprocess on the other
# --------------------------------------------------------------------------

#: Where a docker command's merged output goes, line by line, as it
#: arrives. Merged rather than split because §8 says the two streams
#: **are** one stream: "standard output and standard error together are
#: one raw, opaque log stream".
LineSink = Callable[[str], None]

#: The one impure operation, injectable so the suite never needs docker.
#: A default bound in a signature cannot be replaced by monkeypatching the
#: module, and a test that thinks it stubbed docker out but did not is a
#: test that starts a real build (the container path learned this the hard
#: way) — so it is resolved at call time.
Runner = Callable[[Sequence[str], "LineSink | None"], Completed]


def _run_command(argv: Sequence[str], on_line: LineSink | None = None) -> Completed:
    """Run *argv*, streaming to *on_line* when given, else capturing.

    The only function in this module that talks to a real process. The
    streaming branch is the invocation itself — its output is the build
    log, which has to reach the caller while the build runs rather than as
    a value at the end; every other command is short and wants its output
    as a value.
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


def inspect_command(docker: str, reference: str) -> list[str]:
    """``docker image inspect``, one JSON object per image (§9.1 cross-check)."""
    return [docker, "image", "inspect", "--format", "{{json .}}", reference]


def read_file_command(docker: str, image: str, path: str) -> list[str]:
    """A throwaway ``--rm`` run whose command is ``cat`` — reading one file.

    The cheapest read an image allows a backend that must not depend on
    the program being invocable yet (§2.2.1). ``--network=none`` and no
    mounts: reading a file grants nothing.
    """
    return [docker, "run", "--rm", "--network=none", image, "cat", path]


def start_command(
    *,
    docker: str,
    image: str,
    mounts: Sequence[Mount],
    user: str | None = None,
    limits: ResourceLimits | None = None,
) -> list[str]:
    """The ``docker run`` that gives the build its container.

    * ``--detach`` because the container is the build's *place* for the
      whole invocation — the invocation is a ``docker exec`` into it.
    * ``--init`` because a build spawns hundreds of short-lived children
      and PID 1 has to reap them.
    * ``--network=none`` because §9.1 forbids the network during an
      invocation, and because it is the only way that statement can be
      checked rather than asserted.
    * ``--user`` because everything the program writes lands on a bind
      mount this backend reads back.
    * ``--memory``/``--pids-limit``/optionally ``--cpus`` on the *run*
      that creates the container, because a limit on the exec bounds one
      process tree and a limit on the container bounds the build.
    """
    argv = [docker, "run", "--detach", "--init", "--network=none"]
    if user is not None:
        argv += ["--user", user]
    argv += (limits or ResourceLimits()).to_arguments()
    for mount in _ordered(mounts):
        argv += ["--volume", mount.to_argument()]
    argv.append(image)
    argv += list(IDLE_COMMAND)
    return argv


def exec_command(
    *, docker: str, container: str, action: str, request: Path, user: str | None = None
) -> list[str]:
    """``docker exec`` the program: the contract's whole invocation (§5.1).

    Exactly two positional operands after the program — the action and an
    absolute path to the request document — and never a flag. This argv is
    frozen and never grows: extensibility runs through the request
    document, because an unknown JSON field costs an older program nothing
    while a new argv operand breaks every third-party container.
    """
    argv = [docker, "exec"]
    if user is not None:
        argv += ["--user", user]
    argv += [container, PROGRAM, action, str(request)]
    return argv


def describe_run_command(
    *, docker: str, image: str, mounts: Sequence[Mount], request: Path, user: str | None = None
) -> list[str]:
    """The throwaway ``docker run`` that asks an image what it is.

    ``describe`` is an invocation, so §9.1's "no network during an
    invocation" applies to it — ``--network=none`` is not a nicety here.
    ``--rm`` because the container's only output is the result document on
    the mount, ``--init`` for the same child-reaping reason a build needs
    one, and one ``--volume`` per mount for the probe directory that holds
    the request and result documents.
    """
    argv = [docker, "run", "--rm", "--init", "--network=none"]
    if user is not None:
        argv += ["--user", user]
    for mount in _ordered(mounts):
        argv += ["--volume", mount.to_argument()]
    argv += [image, PROGRAM, ACTION_DESCRIBE, str(request)]
    return argv


def remove_command(docker: str, container: str) -> list[str]:
    """Reap the container. ``--force`` because the build is over either way."""
    return [docker, "rm", "--force", "--volumes", container]


def _ordered(mounts: Sequence[Mount]) -> tuple[Mount, ...]:
    """Bind mounts ordered so a nested one wins over its parent.

    Docker applies bind mounts in the order it is given them, so a mount
    inside another has to come *after* it or the outer one buries it —
    which, for a read-only SDK under a writable parent, is §9.1's
    kernel-enforced write protection silently not happening. The backend
    no longer relies on that nesting (it mounts pieces, not a tree with
    holes), but the ordering costs nothing and keeps a mount set that
    *does* nest correct regardless of caller order.
    """
    return tuple(sorted(mounts, key=lambda mount: len(mount.target.parts)))


class Docker:
    """The container runtime, as the ``local`` method uses it.

    Holds the program name and the one seam function, resolved at call
    time. Every method composes an argv above and runs it; nothing here
    knows about contexts, the SDK or the ABI.
    """

    def __init__(self, program: str = "docker", *, runner: Runner | None = None) -> None:
        self.program = program
        self._runner = runner

    def _invoke(self, argv: Sequence[str], on_line: LineSink | None = None) -> Completed:
        runner = _run_command if self._runner is None else self._runner
        return runner(list(argv), on_line)

    def inspect(self, reference: str) -> ImageProfile | None:
        """One image's facts as an :class:`ImageProfile`, or ``None`` when absent.

        The ``program`` block is left empty here — this call resolves the
        digest and labels (§9.1's cross-check, §2.1's pre-start hint); the
        block is filled by :meth:`describe` once a mount point is known.
        """
        completed = self._invoke(inspect_command(self.program, reference))
        if not completed.ok:
            return None
        facts = _first_json_object(completed.output)
        if facts is None:
            return None
        return ImageProfile(
            reference=reference,
            digest=_repo_digest(facts),
            labels=_labels(facts),
            program={},
        )

    def read_static_describe(self, image: str) -> dict[str, Any] | None:
        """``/mcuhome/describe.json`` out of the image, or ``None`` (§2.2.1)."""
        completed = self._invoke(read_file_command(self.program, image, DESCRIBE_FILE))
        if not completed.ok:
            return None
        return _static_describe(completed.output)

    def describe(self, *, image: str, probe: Path, user: str | None) -> dict[str, Any]:
        """Invoke ``describe`` in a throwaway container, and read the block.

        The fallback of §2.2.1: where the static file is absent or
        unreadable, the backend invokes ``describe`` "exactly as it does
        today". Only the preamble is sent — ``describe`` "needs only
        ``request`` and ``result``, never touches the context, writes
        nothing but the result document".
        """
        request = probe / "request.json"
        result = probe / "result.json"
        write_request({"request": REQUEST_VERSION, "result": str(result)}, request)
        completed = self._invoke(
            describe_run_command(
                docker=self.program,
                image=image,
                mounts=[Mount(source=probe, target=probe)],
                request=request,
                user=user,
            )
        )
        outcome = judge_result(
            result,
            action=ACTION_DESCRIBE,
            exit_code=completed.status,
            session=None,
            context_id=None,
        )
        if not outcome.successful or outcome.result is None:
            raise _image_unusable(image, "; ".join(outcome.problems) or "describe failed")
        program = outcome.result.get("program")
        if not isinstance(program, dict) or not _program_block_complete(program):
            raise _image_unusable(image, "its describe result carries no complete program block")
        return program

    def start(
        self,
        *,
        image: str,
        mounts: Sequence[Mount],
        user: str | None,
        limits: ResourceLimits | None,
    ) -> str:
        """Start the container and answer its id."""
        completed = self._invoke(
            start_command(docker=self.program, image=image, mounts=mounts, user=user, limits=limits)
        )
        identity = (completed.output.strip().splitlines() or [""])[-1]
        if not completed.ok or _CONTAINER_ID.fullmatch(identity) is None:
            raise BuildError(
                f"MCUHome could not start a build container from {image}: "
                f"{_first_line(completed.output)}.",
                hint="check that the image is present and the container runtime is healthy",
            )
        return identity

    def invoke(
        self,
        *,
        container: str,
        action: str,
        request: Path,
        user: str | None,
        on_line: LineSink | None,
    ) -> Completed:
        """``docker exec`` the program, streaming its log to *on_line*."""
        return self._invoke(
            exec_command(
                docker=self.program, container=container, action=action, request=request, user=user
            ),
            on_line,
        )

    def remove(self, container: str) -> None:
        """Reap the container. Never raises: teardown must not become the news."""
        self._invoke(remove_command(self.program, container))


# --------------------------------------------------------------------------
# The request document (§5.2) — backend side
# --------------------------------------------------------------------------


def request_document(
    *,
    result: Path,
    session: str,
    out: Path,
    work: Path,
    tmp: Path,
    context: Path,
    trees: dict[str, TreeEntry],
    jobs: int,
    deadline_seconds: int,
    cancel_grace_seconds: int,
    params: dict[str, Any] | None = None,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The document one working invocation is described by (§5.2).

    Every field §5.2 makes mandatory for a working action is a required
    argument, so a document missing one cannot be composed: ``session``,
    ``out``, ``work``, ``tmp``, ``context``, ``trees.sdk`` and
    ``limits.jobs`` on top of the immortal preamble.

    ``limits.jobs`` is resolved host-side and is authoritative — not a
    hint the program may improve on with ``nproc``, which sees the host
    CPU count but not the RAM budget. ``limits.memory_bytes`` is **not
    written**: it is advisory and this backend enforces memory through the
    runtime, so stating a number it does not enforce would be a promise
    nothing behind it keeps. ``params`` is omitted for an action that has
    none; on ``build`` this backend writes ``mode`` explicitly because it
    also demands the pointer through ``required`` — the value has to be
    there to be honoured (§5.2).

    There is deliberately **no invocation id**: the backend addresses an
    invocation by the ``out``, ``result`` and ``events`` paths it chose,
    so a token the program could only echo back would be one more field
    for a third party to get right for nothing.
    """
    document: dict[str, Any] = {
        # The immortal preamble, first and by name: from here on every
        # error the program hits is a result document, because `result` is
        # guaranteed to be a top-level string path in every future request
        # format version.
        "request": REQUEST_VERSION,
        "result": str(result),
        "session": session,
        "out": str(out),
        "work": str(work),
        "tmp": str(tmp),
        "context": str(context),
        "trees": {name: entry.to_dict() for name, entry in sorted(trees.items())},
        "limits": {
            "jobs": jobs,
            "deadline_seconds": deadline_seconds,
            "cancel_grace_seconds": cancel_grace_seconds,
        },
    }
    if params:
        document["params"] = dict(params)
    if required:
        document["required"] = list(required)
    return document


def write_request(document: dict[str, Any], path: Path) -> None:
    """Place the request document atomically, and durably (§5.1 step 1).

    A temporary neighbour, an ``fsync``, a rename, and an ``fsync`` of the
    directory so the name itself survives. The program opens ``argv[2]``
    and parses it, and a half-written document is the one program-caused
    error that cannot produce a result document.

    UTF-8 without BOM, one JSON object, RFC 8259, and no ``null`` anywhere:
    ``null`` never means "absent" in this document, it is invalid, and
    :func:`request_document` omits rather than nulls.
    """
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False)
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


# --------------------------------------------------------------------------
# The result document (§5.3, §5.4) — backend side
# --------------------------------------------------------------------------


def judge_result(
    path: Path,
    *,
    action: str,
    exit_code: int | None,
    session: str | None,
    context_id: str | None,
    patched_layers: tuple[str, ...] = (),
) -> LocalOutcome:
    """Read the result document if it exists, and judge it (§5.3).

    "The backend reads the result document **if it exists**, regardless of
    the exit code. An invocation is successful exactly when all of the
    following hold": the document parses and names an implemented
    ``result`` version; it carries every §5.4-mandatory field for the
    action; ``action`` echoes ``argv[1]`` and every echo field the request
    supplied echoes correctly; ``status == "success"``; the observed exit
    code is 0; every declared artifact exists and re-hashes; and, for a
    working action, the backend's own context ID matches ``result.context``.

    This function settles everything the document alone can — conditions 1
    to 5 and 7. Condition 6 is egress (§9.3), needs the filesystem, and is
    :func:`verify_artifacts`; the caller folds it in. *context_id* is the
    id **the backend computed itself**; ``result.context`` exists only to
    be compared against it.
    """
    outcome = LocalOutcome(
        action=action, context_id=context_id or "", exit_code=exit_code, result=None
    )
    data = _load(path)
    if data is None:
        # No document at all. §9.1: "an `out` directory without a result
        # document at `result` is a failed invocation" — the case a
        # resource limit that aborted the program produces.
        outcome.problems = ("no result document was written at the path the request named",)
        outcome.status = _STATUS_FAILURE
        return outcome

    outcome.result = data
    problems: list[str] = []
    status = _status(data)
    outcome.status = status

    if data.get("result") != RESULT_VERSION:
        problems.append(
            f"the result document names result format version {data.get('result')!r} and "
            f"this backend implements {RESULT_VERSION}"
        )
    problems.extend(_missing_mandatory(data, action, status, patched_layers))
    if data.get("action") != action:
        problems.append(
            f"the result echoes action {data.get('action')!r} for an invocation of {action!r}"
        )
    if session is not None and data.get("session") != session:
        problems.append(
            f"the result echoes session {data.get('session')!r} for session {session!r}"
        )
    if status != _STATUS_SUCCESS:
        problems.append(f"the program reported status {data.get('status')!r}")
    if exit_code is not None and exit_code != 0:
        problems.append(f"the program exited {exit_code}")
    if context_id is not None and status == _STATUS_SUCCESS and data.get("context") != context_id:
        problems.append(
            "the context id the program computed does not match the one this backend "
            f"computed ({data.get('context')!r} against {context_id!r})"
        )

    outcome.violation = _violation(data, status, exit_code)
    outcome.problems = tuple(problems)
    outcome.successful = not problems
    return outcome


def declared_artifacts(data: dict[str, Any]) -> tuple[tuple[Artifact, ...], tuple[str, ...]]:
    """The declared artifacts, and what was wrong with the rest (§5.4, §9.3).

    Returns ``(resolvable, problems)``. An entry that is "not resolvable"
    — missing any of ``root``/``path``/``role``/``hashes``, or naming a
    ``root`` this version does not know — is skipped silently, exactly as
    §5.4 requires. An entry that carries all four and is still wrong about
    itself — a ``path`` outside §9.2's charset, or a ``sha256`` in any
    rendering other than 64 lowercase hex digits — is a *problem*: §5.3's
    sixth condition failing, which fails the invocation rather than
    quietly shrinking the delivery.
    """
    entries = data.get("artifacts")
    if not isinstance(entries, list):
        return (), ()
    found: list[Artifact] = []
    problems: list[str] = []
    for entry in entries:
        artifact, problem = _artifact(entry)
        if artifact is not None:
            found.append(artifact)
        elif problem is not None:
            problems.append(problem)
    return tuple(found), tuple(problems)


def verify_artifacts(
    out: Path, declared: Sequence[Artifact], *, max_bytes: int | None = None
) -> tuple[tuple[Artifact, ...], tuple[str, ...]]:
    """§5.3 condition 6 and §9.3 egress: re-hash from disk, reject non-files.

    Resolves each declared artifact under ``out`` "without following
    symlinks" — segment by segment with ``lstat`` and **never**
    :meth:`Path.resolve`, which is the security fix. ``resolve`` answers
    where a path *leads*, which is the wrong question: a ``firmware.hex``
    that is a symlink to another file in ``out`` resolves to a
    contained-looking path only after the link has been followed, and the
    followed target would then be re-hashed and served under the declared
    name. What §9.3 asks is that no segment of the path be a link at all,
    which is what :func:`_contained` walks — an in-out symlink is
    *rejected*, not served.

    On the unresolved final path it then rejects a non-regular file, a
    hardlink (``nlink > 1``, a second name for bytes that may live outside
    ``out``), applies the size cap, and **re-hashes from the bytes on
    disk** — declared values are advisory. Serves exactly the intersection
    of declared and verified. Returns ``(verified, problems)``.
    """
    verified: list[Artifact] = []
    problems: list[str] = []
    for entry in declared:
        resolved = _contained(out, entry.path)
        if resolved is None:
            problems.append(
                f'the declared artifact "{entry.path}" is not contained in out: a segment is '
                "absent, a symlink, or leaves the directory"
            )
            continue
        info = _lstat(resolved)
        if info is None:
            problems.append(f'the declared artifact "{entry.path}" is not present under out')
            continue
        if not stat.S_ISREG(info.st_mode):
            problems.append(
                f'the declared artifact "{entry.path}" is not a regular file: a symlink, '
                "device node, FIFO or socket is not a servable artifact"
            )
            continue
        if info.st_nlink > 1:
            problems.append(
                f'the declared artifact "{entry.path}" is a hardlink (nlink {info.st_nlink}): '
                "a second name for bytes that may live outside out"
            )
            continue
        if max_bytes is not None and info.st_size > max_bytes:
            problems.append(f'the declared artifact "{entry.path}" is larger than the size cap')
            continue
        measured = sha256_file(resolved)
        if measured != entry.sha256:
            problems.append(
                f'the declared artifact "{entry.path}" hashes to {measured}, and the result '
                f"declared {entry.sha256}"
            )
            continue
        verified.append(entry)
    return tuple(verified), tuple(problems)


def _contained(out: Path, relative: str) -> Path | None:
    """The absolute path of a declared artifact under ``out``, or ``None``.

    Strict containment, checked segment by segment with ``lstat`` rather
    than with :meth:`Path.resolve`: what matters is that no segment of the
    path is a symlink at all, and that every non-final segment is a real
    directory. A missing final segment is not a failure here — it returns
    the path so the caller can report "declared and not there" distinctly
    from "leaves the directory" — but a missing intermediate segment, a
    symlinked segment, or a ``.``/``..``/empty segment is ``None``.
    """
    segments = relative.split("/")
    if not segments or any(part in ("", ".", "..") for part in segments):
        return None
    current = out
    for part in segments:
        current = current / part
        info = _lstat(current)
        if info is None:
            return current if part == segments[-1] else None
        if stat.S_ISLNK(info.st_mode):
            return None
        if part != segments[-1] and not stat.S_ISDIR(info.st_mode):
            return None
    return current


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except OSError:
        return None


# --------------------------------------------------------------------------
# The SDK package (§6.1, §9.1) — found, verified, unpacked
# --------------------------------------------------------------------------


def acquire_sdk(*, version: str, sha256: str, sources: Sequence[Path], into: Path) -> SdkPackage:
    """Find the pinned SDK package, verify its bytes, unpack it safely.

    §9.1 makes a verified SDK a backend duty: the content of ``trees.sdk``
    matches the manifest's ``mcuhome.package.sha256``, acquired "by (name,
    version, sha256) from operator-configured sources only; the manifest's
    ``package.url`` is a hint, never an instruction". This backend
    implements contract v1's first source tier — local directories,
    searched in order (E48) — so "not here" is a final answer.

    The **hash decides, not the name.** A source directory's ``index.json``
    (``scripts/build_sdk_archive.py``) maps the version to a file; that
    file's bytes are hashed and the value must equal the pin. A file with
    the right name and the wrong bytes is refused exactly as loudly as one
    that is not there. The unpack is the safe extraction of §9.1: regular
    files and directories only, and the executable bit preserved so
    ``bin/generate`` can be spawned (§6.1).
    """
    searched = [str(directory) for directory in sources]
    for directory in sources:
        index_path = directory / INDEX_FILE
        if not index_path.is_file():
            continue
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            resolved = resolve_from_index(index, SDK_PACKAGE_NAME, f"=={version}")
        except BuildError:
            continue
        candidate = directory / resolved.file
        if not candidate.is_file():
            continue
        if resolved.sha256 != sha256:
            raise _sdk_unavailable(
                version,
                sha256,
                searched,
                f"{index_path} lists {resolved.file} with sha256 {resolved.sha256}, and the "
                f"context pins {sha256}",
            )
        measured = sha256_file(candidate)
        if measured != sha256:
            raise _sdk_unavailable(
                version,
                sha256,
                searched,
                f"{candidate} is named for this version and hashes to {measured}",
            )
        into.mkdir(parents=True, exist_ok=True)
        spool = into.parent / f"{into.name}.tar"
        try:
            _decompress(candidate, spool, limit=SDK_MAX_BYTES)
            _safe_extract(spool, into=into, quota_bytes=SDK_MAX_BYTES)
        finally:
            spool.unlink(missing_ok=True)
        return SdkPackage(version=version, sha256=sha256, source=candidate, tree=into)
    raise _sdk_unavailable(
        version, sha256, searched, f"no source directory holds {SDK_PACKAGE_NAME} {version}"
    )


def _decompress(archive: Path, spool: Path, *, limit: int) -> None:
    """zstd to a plain tar on disk, refusing an expansion mid-stream.

    Streaming rather than one-shot: a few kilobytes of zstd can expand to
    gigabytes, and a decompressor that returned its output as one
    ``bytes`` would have allocated the bomb before any check could run.
    """
    written = 0
    with archive.open("rb") as raw, spool.open("wb") as handle:
        reader = zstandard.ZstdDecompressor().stream_reader(raw)
        while block := reader.read(_BLOCK):
            written += len(block)
            if written > limit:
                raise BuildError(
                    f"The SDK package at {archive} unpacks to more than {limit} bytes.",
                    hint="the archive is corrupt or hostile — do not list a source you distrust",
                )
            handle.write(block)


def _safe_extract(archive: Path, *, into: Path, quota_bytes: int) -> None:
    """Safe extraction (§9.1): regular files and directories only.

    Absolute paths, ``..`` after normalization, symlinks, hardlinks and
    device nodes are rejected — each of them is a way out of the directory
    the tree is unpacked into. The archive's mode bits are discarded down
    to two values: 0600, or 0700 for a file the archive marked executable,
    because §6.1 spawns ``bin/generate`` as a child process and an SDK
    unpacked without its exec bit answers exit 127 where code generation
    should be.
    """
    into.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with archive.open("rb") as raw, tarfile.open(fileobj=raw, mode="r|") as tar:
            for member in tar:
                name = _safe_member_name(member.name)
                target = into / name
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise BuildError(
                        f'The SDK package carries "{member.name}", which is not a regular '
                        "file: a symlink, hardlink or device node is a way out of the tree.",
                        hint="an SDK package holds regular files and directories only (§9.1)",
                    )
                source = tar.extractfile(member)
                if source is None:  # pragma: no cover - isfile() was true a line ago
                    raise BuildError(f'The SDK entry "{member.name}" carries no data.', hint="")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as handle:
                    while block := source.read(_BLOCK):
                        written += len(block)
                        if written > quota_bytes:
                            raise BuildError(
                                f"The SDK package unpacks to more than {quota_bytes} bytes.",
                                hint="the archive is corrupt or hostile",
                            )
                        handle.write(block)
                executable = bool(member.mode & 0o100)
                target.chmod(0o700 if executable else 0o600)
    except tarfile.TarError as error:
        raise BuildError(
            f"The SDK package at {archive} is not a readable tar ({error}).",
            hint="the archive is corrupt — re-fetch it or point at another source",
        ) from error
    except OSError as error:
        # A member the tar reads back fine but the filesystem refuses to
        # create — a component over NAME_MAX (ENAMETOOLONG), a path too
        # deep, no space — is not a `TarError` and would otherwise leave
        # this function as a bare `OSError`. It still falls safely, before
        # any container starts, but a typed refusal is the difference
        # between a fix ("the archive is hostile") and a traceback. The
        # `BuildError`s raised above (unsafe path, quota) are not `OSError`
        # and pass through this arm untouched.
        raise BuildError(
            f"The SDK package at {archive} holds an entry this filesystem cannot unpack ({error}).",
            hint="an SDK entry names a path the filesystem rejects — a segment over NAME_MAX, "
            "or a tree too deep; the archive is corrupt or hostile",
        ) from error


def _safe_member_name(name: str) -> str:
    """A tar member's path, or a refusal. Never normalized — refused.

    ``..`` and absolute paths are the escape, and rewriting ``./x`` to
    ``x`` would accept a tree whose entries a stricter reader then
    refuses. The kernel's own ``\\x00`` and the Windows separator are out
    too.
    """
    cleaned = name.rstrip("/")
    usable = (
        cleaned
        and "\\" not in cleaned
        and "\x00" not in cleaned
        and not cleaned.startswith("/")
        and all(part not in ("", ".", "..") for part in cleaned.split("/"))
    )
    if not usable:
        raise BuildError(
            f"The SDK package carries an unsafe path {name!r}.",
            hint=(
                "an SDK entry is a relative path with forward slashes and no empty, . or .. "
                "segment — a traversal is refused, never normalized (§9.1)"
            ),
        )
    return cleaned


# --------------------------------------------------------------------------
# The backend
# --------------------------------------------------------------------------


class LocalBackend:
    """The ``local`` build method: one context, one container, one invocation.

    Given a context directory (already created and locked by
    :mod:`mcuhome.workbench.contextdir`) and an action, :meth:`run` drives
    the whole §5 lifecycle to its end and tears the container down
    whatever the end is. Everything expensive — the SDK fetch, the
    container — happens in :meth:`run`; the constructor holds only the
    config and the docker seam.
    """

    def __init__(self, config: BackendConfig, *, docker: Docker | None = None) -> None:
        self.config = config
        self.docker = Docker() if docker is None else docker

    def run(
        self,
        *,
        context_dir: Path,
        action: str,
        work_root: Path,
        mode: str | None = None,
        on_line: LineSink | None = None,
    ) -> LocalOutcome:
        """Drive one ``verify`` or ``build`` through the container ABI.

        The lifecycle, in the order §5 and §9.1 fix it: read the context's
        pins; resolve the image digest and cross-check it against the pin;
        acquire and verify the SDK; learn the mount layout from
        ``describe``; arrange the mounts piece by piece and start the
        container; prepare the per-invocation directory (empty ``out``,
        empty ``tmp``) and write the request document atomically; exec the
        program; read the result and judge it against the full §5.3
        criteria; and reap the container.
        """
        context_dir = Path(context_dir).resolve()
        manifest = read_context_manifest(context_dir / _MANIFEST_FILE)
        context_id = manifest.compute_id()
        patched = derive_patch_layers(context_dir)
        user = current_user()

        profile = self._resolve_image(manifest)
        if action not in profile.actions:
            raise _image_unusable(
                profile.reference,
                f'it does not implement "{action}"; it announced {sorted(profile.actions)}',
            )

        work_root = Path(work_root).resolve()
        work_root.mkdir(parents=True, exist_ok=True)
        sdk_tree = work_root / "sdk"
        package = acquire_sdk(
            version=manifest.sdk.version,
            sha256=manifest.sdk.sha256,
            sources=self.config.sdk_sources,
            into=sdk_tree,
        )

        # §9.1: every invocation gets a **fresh, empty** `out` and `tmp`;
        # `work` is "the session's persistent working area" and must
        # survive between invocations, because that is the whole of what
        # `mode: incremental` has to reason about. So `work` is created
        # once and reused, and the invocation directory — which holds
        # `out`, `tmp` and this run's request/result documents — is wiped
        # and recreated. Without this, a second `run()` on the same
        # `work_root` would inherit the previous invocation's `out`: an
        # old `out/firmware.hex` that still matched a re-declared hash
        # would let a later non-conforming build slip through egress, and
        # a stale `result.json` would be judged as this run's answer.
        work = work_root / "work"
        invocation = work_root / "inv"
        work.mkdir(parents=True, exist_ok=True)
        if invocation.exists():
            shutil.rmtree(invocation)
        out = invocation / "out"
        tmp = invocation / "tmp"
        out.mkdir(parents=True)
        tmp.mkdir(parents=True)

        trees, mounts = self._arrange_trees(
            profile, package, patched, context=context_dir, work=work, invocation=invocation
        )
        container = self.docker.start(
            image=profile.reference,
            mounts=mounts,
            user=user,
            limits=self.config.limits(),
        )
        try:
            request = invocation / "request.json"
            result = invocation / "result.json"
            document = self._document(
                action=action,
                result=result,
                out=out,
                work=work,
                tmp=tmp,
                context=context_dir,
                trees=trees,
                patched=patched,
                mode=mode,
            )
            write_request(document, request)
            completed = self.docker.invoke(
                container=container,
                action=action,
                request=request,
                user=user,
                on_line=on_line,
            )
            return self._collect(
                result,
                out=out,
                action=action,
                exit_code=completed.status,
                session=document["session"],
                context_id=context_id,
                patched=patched,
            )
        finally:
            self.docker.remove(container)

    def _resolve_image(self, manifest: ContextManifest) -> ImageProfile:
        """The image the manifest records, resolved and cross-checked (§9.1).

        The reference is resolved **by this backend's own** ``docker image
        inspect`` (E51: the workbench runs its own inspect; there is no
        capabilities verb, which is the remote method's). Two things are
        then cross-checked against the image actually found, and they are
        the two halves E61 separates:

        * the **requirement** — ``manifest.zephyr``, what the context
          asked for — against the image's ``org.mcuhome.zephyr`` label.
          This is the check that matters: a context is built by a
          container of the line its model was resolved against, or it is
          not built.
        * the **resolution** — ``manifest.container.digest``, what a
          backend recorded when it locked this context — against the
          digest the image reports now. A ``None`` on either side is
          tolerated: an image built locally and never pushed names no
          pinnable bytes, which is a fact about it rather than a
          mismatch.

        The second check is a weaker statement than contract v1's, and
        deliberately so: under v2 the digest is this side's own record
        rather than the client's demand, so what it catches is a manifest
        that names one image while this backend is about to invoke
        another — a build attributed to the wrong environment.
        """
        recorded = manifest.container
        reference = self.config.image or recorded.reference()
        facts = self.docker.inspect(reference)
        if facts is None:
            raise BuildError(
                f"No build container on this host answers to {reference}.",
                hint=(
                    "the local build method resolves the recorded image against this "
                    "host's images and pulls nothing — pull or build the image the "
                    "manifest names, or point --image at it"
                ),
            )
        offered = facts.labels.get(ZEPHYR_LABEL) or ""
        if not satisfies_line(offered, line=manifest.zephyr):
            # An absent label and a wrong one are one refusal with two
            # sentences: both mean "this image does not serve that line"
            # (§2.1.1: absence is never read as compatible), and naming
            # the label rather than an empty value is what tells the
            # operator of an unlabelled image what to fix.
            says = f"carries Zephyr {offered}" if offered else f"carries no {ZEPHYR_LABEL} label"
            raise BuildError(
                f"The build container {reference} {says}, and this context needs the "
                f"{manifest.zephyr} line.",
                hint=(
                    "a context states the Zephyr line its model was resolved against and "
                    "the backend picks a container of that line (E61) — this image is not "
                    "one. Point --image at a container of that line, or rebuild the "
                    "context for a line this host serves."
                ),
            )
        known = facts.digest is not None and recorded.digest is not None
        if known and facts.digest != recorded.digest:
            raise BuildError(
                f"The image {reference} reports digest {facts.digest}, and this context's "
                f"manifest records {recorded.digest}.",
                hint=(
                    "the manifest records which container a backend resolved this context "
                    "to; this host answers that name with different bytes"
                ),
            )
        static = self.docker.read_static_describe(reference)
        if static is not None:
            program = static.get("program") if isinstance(static, dict) else None
            if isinstance(program, dict) and _program_block_complete(program):
                return self._profile(reference, facts, program)
        probe = _probe_directory()
        try:
            program = self.docker.describe(image=reference, probe=probe, user=current_user())
        finally:
            shutil.rmtree(probe, ignore_errors=True)
        return self._profile(reference, facts, program)

    def _profile(
        self, reference: str, facts: ImageProfile, program: dict[str, Any]
    ) -> ImageProfile:
        """Gate a ``describe`` answer against §7.1.1 before it becomes a profile.

        The pre-invocation gate, run on **both** the static ``describe.json``
        path and the invoked-``describe`` path, because §7.1.1 makes the
        check a precondition of invoking any working action rather than of
        how the block was obtained: "A backend that does not implement the
        value it finds here MUST NOT invoke a working action on this
        program." Field presence alone is not that gate — a program block
        can be complete and still name a contract, a request format or a
        result format this backend cannot speak, and a build invoked on it
        would read a result document described by a specification this side
        does not have.

        The refusal is :func:`_image_unusable` and happens here, before
        ``acquire_sdk`` and before the container is started — the label
        contradiction §7.1.1 calls "a contract violation against the image"
        included, which this backend surfaces as the same clean refusal
        rather than as a crash from inside a build.
        """
        problem = _program_problem(program, facts.labels)
        if problem is not None:
            raise _image_unusable(reference, problem)
        return ImageProfile(
            reference=reference, digest=facts.digest, labels=facts.labels, program=program
        )

    def _arrange_trees(
        self,
        profile: ImageProfile,
        package: SdkPackage,
        patched: tuple[str, ...],
        *,
        context: Path,
        work: Path,
        invocation: Path,
    ) -> tuple[dict[str, TreeEntry], list[Mount]]:
        """Every ``trees`` entry and the mounts behind them (§4.1, E47).

        The session tree is mounted **piece by piece and never wholesale**:
        ``context`` read-only, ``work`` writable, the per-invocation
        directory writable (mounted as itself so ``out``/``tmp``/request/
        result inside it are visible), and the SDK read-only at the path
        ``describe`` declared for it — or at its unpack path when
        ``describe`` declared ``null``. A wholesale root mount would expose
        the SDK writable at its unpack path and make ``writable: false`` a
        false claim, which §9.1 forbids.

        The SDK is mounted writable only when the ``sdk`` layer carries
        patches, in which case the per-session unpacked tree *is* the
        writable view §6.2 asks for. Every other patched in-image layer is
        asserted ``writable: true`` at the path ``describe`` reported, with
        **no mount** (E47): the container's own copy-on-write layer is the
        view, and the container is discarded when the build ends. A patched
        layer the image reports no path for gets no entry — the pointer
        still goes into ``required`` (:meth:`_document`) so a conforming
        program refuses legibly.
        """
        mounts = [
            Mount(source=context, target=context, read_only=True),
            Mount(source=work, target=work),
            Mount(source=invocation, target=invocation),
        ]
        sdk_writable = "sdk" in patched
        sdk_target = profile.tree_path("sdk") or package.tree
        mounts.append(Mount(source=package.tree, target=sdk_target, read_only=not sdk_writable))
        trees: dict[str, TreeEntry] = {"sdk": TreeEntry(path=sdk_target, writable=sdk_writable)}
        for layer in patched:
            if layer == "sdk":
                continue
            declared = profile.tree_path(layer)
            if declared is None:
                continue
            trees[layer] = TreeEntry(path=declared, writable=True)
        return trees, mounts

    def _document(
        self,
        *,
        action: str,
        result: Path,
        out: Path,
        work: Path,
        tmp: Path,
        context: Path,
        trees: dict[str, TreeEntry],
        patched: tuple[str, ...],
        mode: str | None,
    ) -> dict[str, Any]:
        """The request document for one invocation (§5.2).

        ``build`` demands ``/params/mode`` and ``/trees/<layer>`` for every
        patched layer through ``required``, and states ``mode`` explicitly
        so the demanded value is there to honour. ``verify`` demands
        neither — it "applies no patches and touches no source tree", so
        demanding a tree pointer would ask a conforming program to refuse
        for not using something it is forbidden to use — but the tree
        entries are still supplied (§7.3): "a view it never writes to is
        indistinguishable from one it was not given".
        """
        params: dict[str, Any] | None = None
        required: list[str] = []
        if action == ACTION_BUILD:
            params = {"mode": mode or "clean"}
            required.append("/params/mode")
            required += [f"/trees/{layer}" for layer in patched]
        return request_document(
            result=result,
            session=f"local-{uuid.uuid4().hex[:12]}",
            out=out,
            work=work,
            tmp=tmp,
            context=context,
            trees=trees,
            jobs=self.config.jobs,
            deadline_seconds=self.config.deadline_seconds,
            cancel_grace_seconds=self.config.cancel_grace_seconds,
            params=params,
            required=tuple(required),
        )

    def _collect(
        self,
        result: Path,
        *,
        out: Path,
        action: str,
        exit_code: int | None,
        session: str,
        context_id: str,
        patched: tuple[str, ...],
    ) -> LocalOutcome:
        """Read the result, harden egress, and decide what it was worth.

        §5.3's seventh condition and §9.3's re-hashing meet here: the
        document is judged by :func:`judge_result` and the artifacts by
        :func:`verify_artifacts`, and an invocation is successful only if
        both had nothing to say. A declared entry that is malformed, one
        whose bytes do not survive re-hashing, and §7.2's delivery rule
        (a successful build delivers one ``firmware`` and exactly one
        ``report``) all fail the invocation.
        """
        outcome = judge_result(
            result,
            action=action,
            exit_code=exit_code,
            session=session,
            context_id=context_id,
            patched_layers=patched,
        )
        outcome.out = out
        if outcome.result is not None:
            declared, malformed = declared_artifacts(outcome.result)
            verified, problems = verify_artifacts(out, declared)
            outcome.artifacts = verified
            problems = (
                malformed
                + problems
                + _delivery_problems(action, outcome.result, outcome.status, verified)
            )
            if problems:
                outcome.problems = outcome.problems + problems
                outcome.successful = False
        return outcome


def derive_patch_layers(context_dir: Path) -> tuple[str, ...]:
    """The layers the context carries patches for, from the paths alone.

    ADR 0018 decision 2: a patch's layer **is** its subfolder, and there
    is no declared patch list to disagree with the files present. So the
    patched set is the sorted names of the non-empty ``patches/<layer>/``
    directories, re-derived rather than recorded.
    """
    patches = Path(context_dir) / "patches"
    if not patches.is_dir():
        return ()
    layers = [
        entry.name
        for entry in sorted(patches.iterdir())
        if entry.is_dir() and any(child.is_file() for child in entry.iterdir())
    ]
    return tuple(layers)


# --------------------------------------------------------------------------
# Reading helpers, kept small and private
# --------------------------------------------------------------------------

_STATUS_SUCCESS = "success"
_STATUS_FAILURE = "failure"
_STATUS_UNSUPPORTED = "unsupported"
_STATUS_CANCELLED = "cancelled"
_STATUSES = (_STATUS_SUCCESS, _STATUS_FAILURE, _STATUS_UNSUPPORTED, _STATUS_CANCELLED)

_MANIFEST_FILE = "manifest.yaml"


def _status(data: dict[str, Any]) -> str:
    """The status, treating an unknown value as failure (§5.4)."""
    found = data.get("status")
    return found if found in _STATUSES else _STATUS_FAILURE


def _load(path: Path) -> dict[str, Any] | None:
    """The result document as an object, or ``None`` if there is no usable one."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _missing_mandatory(
    data: dict[str, Any], action: str, status: str, patched_layers: tuple[str, ...]
) -> tuple[str, ...]:
    """§5.4's per-action table, as a list of what is wrong with a document."""
    missing: list[str] = []
    success = status == _STATUS_SUCCESS
    working = action in (ACTION_VERIFY, ACTION_BUILD)
    if "status" not in data:
        missing.append("the result document carries no status")
    if "action" not in data:
        missing.append("the result document carries no action")
    if status in (_STATUS_FAILURE, _STATUS_UNSUPPORTED):
        if not isinstance(data.get("reason"), str):
            missing.append(f"a {status} result carries no reason")
        if not isinstance(data.get("error"), dict):
            missing.append(f"a {status} result carries no error object")
    if status == _STATUS_CANCELLED and (
        data.get("reason") is not None or data.get("error") is not None
    ):
        missing.append("a cancelled result carries a reason or an error, and carries neither")
    if working and success and "context" not in data:
        missing.append(f"a successful {action} carries no context id")
    if action == ACTION_BUILD and success:
        if not isinstance(data.get("artifacts"), list):
            missing.append("a successful build declares no artifacts, and an absent list is empty")
        if not isinstance(data.get("layers"), dict):
            missing.append("a successful build carries no layers block")
        else:
            missing.extend(_layer_problems(data["layers"], patched_layers))
    if action == ACTION_VERIFY and "layers" in data:
        missing.append("a verify result carries a layers block, which verify may not report")
    if action == ACTION_DESCRIBE:
        if not isinstance(data.get("program"), dict):
            missing.append("a describe result carries no program block")
        forbidden = [name for name in ("context", "artifacts", "layers") if name in data]
        if forbidden:
            missing.append(
                f"a describe result carries {', '.join(forbidden)}, which it cannot have measured"
            )
    return tuple(missing)


def _layer_problems(layers: dict[str, Any], patched: tuple[str, ...]) -> tuple[str, ...]:
    """``layers`` against the patch set the backend derived (§5.4)."""
    problems: list[str] = []
    for layer in patched:
        if layer not in layers:
            problems.append(
                f'a successful build reports no layers entry for "{layer}", whose patches '
                "the context carries"
            )
    for layer in sorted(layers):
        if layer not in patched:
            problems.append(
                f'a successful build reports a layers entry for "{layer}", which this '
                "context does not patch"
            )
    return tuple(problems)


def _violation(data: dict[str, Any], status: str, exit_code: int | None) -> str | None:
    """The contract violation §5.3 raises against the image, if any."""
    if exit_code is not None and exit_code not in (0, 1, 66):
        return f"the program exited {exit_code}, which is outside the frozen set 0, 1 and 66"
    if exit_code == 0 and status != _STATUS_SUCCESS:
        return f"the program exited 0 and its result says {data.get('status')!r}"
    if exit_code == 1 and status == _STATUS_SUCCESS:
        return "the program exited 1 and its result says success"
    if exit_code == 66 and data:
        return "the program exited 66, which means no result could be addressed, and wrote one"
    program = data.get("program")
    if isinstance(program, dict) and program and not _program_block_complete(program):
        return "the result carries an incomplete program block, which is not discovery data"
    return None


def _delivery_problems(
    action: str, data: dict[str, Any], status: str, verified: Sequence[Artifact]
) -> tuple[str, ...]:
    """§7.2's delivery rule, measured on what survived egress.

    "A successful device build MUST declare at least two artifacts: the
    unsigned image with role ``firmware`` … and **exactly one artifact
    with role ``report``**." Measured on the *verified* set, because that
    is what the caller receives: a report declared and not verified has
    produced an image nobody can sign just as surely as one never
    declared.
    """
    if action != ACTION_BUILD or status != _STATUS_SUCCESS:
        return ()
    problems: list[str] = []
    if not any(entry.role == "firmware" for entry in verified):
        problems.append("a successful build delivers no artifact with role firmware")
    reports = sum(1 for entry in verified if entry.role == "report")
    if reports != 1:
        problems.append(
            f"a successful build delivers {reports} artifacts with role report, and the "
            "client that signs detached needs exactly one"
        )
    return tuple(problems)


def _artifact(entry: Any) -> tuple[Artifact | None, str | None]:
    """One ``artifacts[]`` entry as ``(artifact, problem)``; at most one."""
    if not isinstance(entry, dict):
        return None, None
    root = entry.get("root")
    path = entry.get("path")
    role = entry.get("role")
    hashes = entry.get("hashes")
    if not isinstance(root, str) or root != ROOT_OUT:
        return None, None
    if not isinstance(path, str) or not isinstance(role, str) or not isinstance(hashes, dict):
        return None, None
    segments = path.split("/")
    if not segments or any(_PATH_SEGMENT.fullmatch(part) is None for part in segments):
        return None, (
            f'the declared artifact path "{path}" is not a relative path of '
            "[A-Za-z0-9._-]+ segments under out"
        )
    if ".." in segments or "." in segments:
        return None, (f'the declared artifact path "{path}" carries a . or .. segment')
    sha256 = hashes.get("sha256")
    if not isinstance(sha256, str):
        return None, None
    if _SHA256_HEX.fullmatch(sha256) is None:
        return None, (
            f'"{path}" declares its sha256 as {sha256!r}, and the one legal spelling is '
            "64 lowercase hex digits with no prefix"
        )
    return Artifact(root=root, path=path, role=role, sha256=sha256), None


def _program_block_complete(program: dict[str, Any]) -> bool:
    return all(name in program for name in PROGRAM_FIELDS)


def _program_problem(program: dict[str, Any], labels: dict[str, str]) -> str | None:
    """Everything §7.1.1 makes hold about a ``describe`` before it is used.

    Every field of the block is mandatory in a ``describe`` result,
    ``trees`` included, and the gates that follow are the ones a backend
    passes before it may invoke anything: a contract version it implements,
    a request format the program parses, a result format it writes, and —
    §2.1 — labels that do not contradict what the block just said. ``None``
    means the image can serve; a string is why it cannot.
    """
    missing = [name for name in (*PROGRAM_FIELDS, "trees") if name not in program]
    if missing:
        return f"its describe result has no program.{', program.'.join(missing)}"
    if program.get("contract") != CONTRACT_VERSION:
        return (
            f"it implements contract version {program.get('contract')!r} and this backend "
            f"implements {CONTRACT_VERSION}"
        )
    if REQUEST_VERSION not in _versions(program.get("request")):
        return (
            f"it parses request format versions {program.get('request')!r} and this backend "
            f"writes {REQUEST_VERSION}"
        )
    if RESULT_VERSION not in _versions(program.get("result")):
        return (
            f"it writes result format versions {program.get('result')!r} and this backend "
            f"reads {RESULT_VERSION}"
        )
    return _label_problem(program, labels)


def _label_problem(program: dict[str, Any], labels: dict[str, str]) -> str | None:
    """The §2.1 cross-check: the image labels against what ``describe`` said.

    "A backend MUST verify them against ``describe`` and MUST NOT rely on a
    label ``describe`` contradicts", and §7.1.1 goes further for the one
    label with a counterpart in the block: ``program.contract`` "MUST equal
    the ``org.mcuhome.contract`` label; where the two disagree, ``describe``
    is authoritative and the disagreement is a contract violation against
    the image".

    The other two labels have no counterpart to check against, so what is
    checked is that they are **present** — they are the coupling labels a
    compatibility constraint is written over, and "a container that does
    not carry a named label does not qualify — absence is never read as
    compatible" (§2.1.1).
    """
    coupling = (CONTRACT_LABEL, ZEPHYR_LABEL, TOOLCHAIN_LABEL)
    absent = [name for name in coupling if not labels.get(name)]
    if absent:
        return f"it carries no {' and no '.join(absent)} label"
    declared = labels[CONTRACT_LABEL]
    if declared != str(program.get("contract")):
        return (
            f"its {CONTRACT_LABEL} label says {declared!r} and its describe result says "
            f"{program.get('contract')!r}"
        )
    return None


def _versions(value: Any) -> tuple[int, ...]:
    """The integer format versions in a ``request``/``result`` list.

    ``bool`` is excluded explicitly: it is an ``int`` in Python, and a
    ``true`` in the list would otherwise read as version 1.
    """
    if not isinstance(value, list):
        return ()
    return tuple(entry for entry in value if isinstance(entry, int) and not isinstance(entry, bool))


def _static_describe(text: str) -> dict[str, Any] | None:
    """A static ``describe.json`` as a result document, or ``None`` (§2.2.1)."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("status") != _STATUS_SUCCESS:
        return None
    program = data.get("program")
    if not isinstance(program, dict) or not _program_block_complete(program):
        return None
    return data


def _first_json_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in ``docker image inspect --format {{json .}}``."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            data = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _repo_digest(facts: dict[str, Any]) -> str | None:
    """The repo digest out of ``RepoDigests`` — the value a manifest records.

    ``Id`` is the local image ID and never compares equal to a manifest's
    ``container.digest``, so it is not read here. ``None`` for an image
    that was built locally and never pushed, which is recorded as ``None``
    rather than repaired.
    """
    digests = facts.get("RepoDigests")
    if isinstance(digests, list):
        for entry in digests:
            _, _, candidate = str(entry).partition("@")
            if candidate:
                return candidate
    return None


def _labels(facts: dict[str, Any]) -> dict[str, str]:
    config = facts.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


def _probe_directory() -> Path:
    return Path(tempfile.mkdtemp(prefix="mcuhome-describe-"))


def _first_line(output: str) -> str:
    for line in output.splitlines():
        if line.strip():
            return line.strip()
    return "no output"


def _image_unusable(reference: str, problem: str) -> BuildError:
    """An image that cannot be trusted with a build, and why.

    ``describe`` is authoritative about what a build container can do and
    doubles as the first conformance test, so an image that cannot answer
    it conformingly is not one a build is invoked on.
    """
    return BuildError(
        f"The image {reference} cannot serve a local build: {problem}.",
        hint="describe is the first conformance test — a build container must answer it",
    )


def _sdk_unavailable(
    version: str, sha256: str, searched: Sequence[str], problem: str
) -> BuildError:
    """The SDK pin names a package this host has not — the ``sdk.unavailable`` spirit.

    Not retryable in spirit: contract v1's first source tier is local
    directories and fetches nothing, so the same command a second later
    searches the same directories and finds the same nothing. What changes
    the answer is an operator putting the package where the backend looks.
    """
    listed = ", ".join(searched) or "none"
    return BuildError(
        f"MCUHome cannot supply the SDK package this context pins ({problem}).",
        hint=(
            f"the local build method reads the SDK from configured source directories only "
            f"and never from the url in the context — add {SDK_PACKAGE_NAME} {version} "
            f"(sha256 {sha256}) to one of: {listed}"
        ),
    )
