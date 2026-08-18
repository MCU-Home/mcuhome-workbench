# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The three build methods behind one interface (ADR 0020 decision 6, E18).

``local-dev`` compiles on the caller's own machine, ``local`` drives a
build container through the invocation ABI, ``remote`` drives a build
server through the session protocol. They differ in almost everything —
one starts a subprocess, one starts a container, one opens a WebSocket —
and in exactly the thing a caller cares about they do not differ at all:
every one of them delivers an **unsigned** image plus a build report, and
the signature is a separate host-side step afterwards (E55, E56). That is
what makes one interface possible rather than merely tidy.

So this module is small on purpose. :func:`build_firmware` takes a
resolved device model plus the inputs a build needs, runs the one the
target names, and answers with one :class:`BuildOutcome` whose meaning
does not depend on which ran: *did it succeed*, *where are the unsigned
artifacts and the report*, and *which report shape is it* — enough for
the shared signing step, and enough for a caller that only wants to know
whether to carry on. What is genuinely composition-specific — a west plan
and its log, a container reference, an invocation id — travels in
:attr:`BuildOutcome.detail`, typed as itself, so a renderer can reach it
without every consumer having to.

**Two axes, and a name is one word for both.** Where a build runs and how
the machine that runs it executes it are separate decisions and only the
first belongs to a caller, which is what :mod:`…buildtarget` states:
:class:`~mcuhome.workbench.buildtarget.LocalBuild` carries an
:class:`~mcuhome.workbench.buildtarget.Execution`,
:class:`~mcuhome.workbench.buildtarget.RemoteBuild` deliberately carries
none. :func:`build_firmware` is the seam that takes one of those, and
:func:`run_build` is the name-shaped entry point over it for a caller
whose method arrived as a flag or a configuration value —
:func:`target_for_method` is the whole of the translation, in one place,
so that the method-specific fields of :class:`BuildRequest` have exactly
one reader.

**No key of any kind can be private here.** Two fields carry key
material and both are public halves in the form their method needs:
:attr:`BuildRequest.signing_pub` is the PEM that becomes
``keys/signing.pub`` in a build context, and
:attr:`BuildRequest.bootloader_key` is the public key file ``west``
compiles into MCUboot. There is no slot a private key fits in — on any
method, by construction — which is the structural half of the invariant
that the signing key never leaves the machine ``mcuhome`` runs on.

**Why the dispatch is here and the methods are not.** ADR 0020 decision 1
gives this package the three build methods; decision 3 forbids it a
*dependency* on :mod:`mcuhome.compiler`, which is where two of the three
are implemented (a dashboard install must not carry a toolchain, ADR 0017
§2, and E32 records that parked tension). Both hold at once because the
edge to the compiler is resolved at call time and refuses in words when
the distribution is absent — the same shape
:mod:`mcuhome.workbench.sessionclient` uses for the ``remote`` extra.
That is also why the import is written through
:func:`importlib.import_module` and not as an ``import`` statement:
``tests_py/test_packaging.py`` reads the dependency arrows out of the
syntax tree, and an ``import`` here would be indistinguishable from the
hard edge that is forbidden.

**Awaitable, because builds wait** (E16, E21). ``remote`` is asynchronous
throughout and the other two block for minutes in a subprocess, so the
one interface over them is ``async`` and the synchronous methods are
offloaded to a thread. A command line wraps the whole thing in one
:func:`asyncio.run` at its entry point and its user sees nothing.

**All three methods are complete** (E65). ``remote`` was the last one
that could not start from a device model: a build context is
content-addressed over the SDK package's hash, and nothing resolved that
pin on this path. It does now, through the *same* resolver and the same
context writer the ``local`` method uses
(:func:`~mcuhome.workbench.resolve_pins.resolve_sdk`,
:func:`~mcuhome.workbench.contextdir.create_build_context`), from the
same ``--sdk-sources`` directories. There is deliberately **no**
capabilities round trip for it, exactly as there is none for the
container since E61: the client states a version *and* a sha256, the
server resolves the version against its own sources — which may be a
cache, a package service or a private registry — and verifies the bytes
it found against the hash. Same number, other bytes is a typed refusal
on that side, never a quiet build against another SDK; and because the
hash is what the identity is computed over, none of this changes the
context format or the ID rule.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from mcuhome.model.artifacts import Artifact
from mcuhome.model.context import ContainerResolution
from mcuhome.model.errors import BuildError
from mcuhome.model.manifest import MANIFEST_FILE
from mcuhome.model.model import DeviceModel

from mcuhome.workbench.buildlock import build_lock
from mcuhome.workbench.buildtarget import (
    DEFAULT_MAX_WAIT_SECONDS,
    BuildTarget,
    ContainerExecution,
    Execution,
    LocalBuild,
    RemoteBuild,
    WorkspaceExecution,
)
from mcuhome.workbench.contextdir import context_facts, create_build_context, lock_context
from mcuhome.workbench.imgtool import BUILD_REPORT_FILE

__all__ = [
    "DEFAULT_MAX_WAIT_SECONDS",
    "LOCAL",
    "LOCAL_DEV",
    "METHODS",
    "REMOTE",
    "BuildOutcome",
    "BuildRequest",
    "BuildTarget",
    "ContainerExecution",
    "Execution",
    "LocalBuild",
    "MethodUnavailable",
    "RemoteBuild",
    "RemoteNotConfigured",
    "UnknownMethod",
    "WorkspaceExecution",
    "build_firmware",
    "websocket_url",
    "resolve_method",
    "run_build",
    "target_for_method",
]

#: Drives a build container on this machine through the invocation ABI
#: (E51). The default: it is the method that needs a container runtime and
#: nothing else of a toolchain, and the one whose private key never
#: reaches the thing that compiles (E54).
LOCAL = "local"

#: Compiles in the caller's own west workspace with the caller's own
#: tools. MCUHome's development loop, and what the other two run *inside*
#: whatever environment they reach (ADR 0020 decision 7).
LOCAL_DEV = "local-dev"

#: The same as ``local``, through a build server (ADR 0019).
REMOTE = "remote"

#: Every method name, in the order a refusal lists them.
METHODS = (LOCAL, LOCAL_DEV, REMOTE)

#: What a caller that expressed no preference gets (E54).
DEFAULT_METHOD = LOCAL

LineSink = Callable[[str], None]


class UnknownMethod(BuildError):
    """A build method by a name that is not one of :data:`METHODS`."""


class MethodUnavailable(BuildError):
    """The named method is real, and this installation cannot run it.

    Worded like every other missing-piece refusal in the family — state
    the fact, then name the exact install — because that is what it is:
    ``local`` and ``local-dev`` are implemented in ``mcuhome-compiler``,
    which a workbench that only validates configurations does not carry.
    """


#: The port a build server listens on unless its operator moved it
#: (``mcuhome-build-server --port``). An address without one means "the
#: usual place", which is what makes `--build-server attic` a complete
#: answer.
DEFAULT_SERVER_PORT = 8100

#: Where the session protocol lives on a build server. One endpoint, so
#: an address never has to carry a path.
SERVER_ENDPOINT = "/ws"


def websocket_url(address: str) -> str:
    """A build server's address, as the URL a client connects to.

    A builder carries a "server address (IP/hostname[:port])" and that is
    what a person types — but what the socket needs is a full WebSocket
    URL, and nothing bridged the two: the address travelled verbatim into
    the connect call, where ``attic:8137`` reads as a URL whose *scheme*
    is ``attic`` and fails with a traceback instead of a refusal.

    Accepted, because all four are things an operator will reasonably
    write down: a bare host, ``host:port``, and either spelling with a
    scheme (``ws``/``wss``, or ``http``/``https`` — which is what a
    browser address bar hands you, and which differ from the first pair
    in nothing but the name). Anything else is refused by name rather
    than by traceback.
    """
    stated = (address or "").strip()
    scheme, separator, rest = stated.partition("://")
    if separator and not scheme:
        scheme, rest = "ws", ""  # "://attic" — the host refusal below is the honest one
    elif not separator:
        # No scheme at all. Deliberately not decided by a URL parser:
        # ``urlsplit("attic:8137")`` reads ``attic`` as the scheme,
        # because a scheme is any word followed by a colon.
        scheme, rest = "ws", stated
    elif scheme in ("http", "https"):
        scheme = "ws" if scheme == "http" else "wss"
    elif scheme not in ("ws", "wss"):
        raise RemoteNotConfigured(
            f'"{stated}" is not a build server address: {scheme} is not one of the '
            f"schemes a build server speaks.",
            hint=(
                "write the address as <host> or <host:port> — a scheme is optional, "
                "and only ws://, wss://, http:// and https:// are understood"
            ),
        )
    host, _, path = rest.partition("/")
    if not host:
        raise RemoteNotConfigured(
            f'"{stated}" names no build server host.',
            hint="write the address as <host> or <host:port>, for example attic:8100",
        )
    if ":" not in host.rpartition("]")[2]:  # a bare host, or a bracketed IPv6 one
        host = f"{host}:{DEFAULT_SERVER_PORT}"
    return f"{scheme}://{host}{SERVER_ENDPOINT if not path else '/' + path}"


class RemoteNotConfigured(BuildError):
    """``remote`` was selected and something it cannot invent is missing.

    Two shapes, and they are the two decisions this method cannot make
    for a caller: **where** to build — there is no default build server
    and no discovery — and **which SDK package** the context pins, which
    is resolved from the caller's own sources and is part of the
    identity the work is attributed to. Both refusals name the knob that
    supplies the value rather than guessing at one, because a guess here
    is either a context sent to a stranger or an identity that describes
    a build nobody asked for.
    """


def resolve_method(name: str | None) -> str:
    """The build method *name* selects, or a refusal listing the real ones.

    ``None`` and the empty string mean "no preference" and resolve to
    :data:`DEFAULT_METHOD`, so a caller can hand through whatever its own
    configuration ladder produced without checking it first.
    """
    if not name:
        return DEFAULT_METHOD
    if name in METHODS:
        return name
    raise UnknownMethod(
        f'"{name}" is not a build method MCUHome knows.',
        hint=(
            "the build methods are "
            + ", ".join(METHODS)
            + f": {LOCAL} compiles in a build container on this "
            f"machine, {LOCAL_DEV} in your own west workspace, and {REMOTE} on a build "
            "server"
        ),
    )


@dataclass(frozen=True)
class BuildRequest:
    """Everything a build method may be given, whichever one runs.

    :attr:`model` and :attr:`out_dir` are the two every method needs: what
    to build, and the durable directory the unsigned artifacts and the
    build report end up beside. Everything below them is optional and
    named for the method it serves; a field a method does not use is
    ignored rather than refused, because a caller assembling one request
    for a method chosen at run time should not have to assemble three.

    The fields marked for one method are the ones a build target states
    instead (:mod:`mcuhome.workbench.buildtarget`), and while both exist
    they are read in exactly one place — :func:`target_for_method`, which
    is what :func:`run_build` turns a method name into. A caller that
    builds a target itself and calls :func:`build_firmware` puts those
    values on the target, and what it leaves here is ignored.
    """

    #: The canonical device model, stages 1-3 already run.
    model: DeviceModel
    #: The build directory: where a user looks afterwards, and where the
    #: shared signing step reads the report from.
    out_dir: Path
    #: The environment to resolve tools, images and caches from — stated,
    #: never read from the process (:mod:`mcuhome.model.userpaths`).
    env: Mapping[str, str] = field(default_factory=dict)
    #: Parallel compile jobs.
    jobs: int = 1
    #: ``clean`` or the pristine mode a method understands; ``local`` and
    #: ``remote`` pass it into the invocation, ``local-dev`` derives its
    #: own from the state of the build directory.
    mode: str = "clean"
    #: Where the build log goes, line by line, while it happens.
    on_line: LineSink | None = None
    #: Called with a step key when the build enters a new step —
    #: ``"context"`` when the build context is being created,
    #: ``"compile"`` when the build environment starts compiling. The
    #: honest-progress seam of cli ADR 0004: a caller renders steps it
    #: was told about, never ones it guessed. Keys are append-only
    #: vocabulary; consumers ignore keys they do not know.
    #:
    #: A step may be reported a second time with keyword **facts** once
    #: it knows something worth stating — which SDK the context pinned,
    #: which image answered the Zephyr requirement. Facts are display
    #: material and append-only in the same way: a consumer renders what
    #: it recognizes and ignores the rest, and a build method that has
    #: nothing to say states nothing rather than inventing it.
    on_step: Callable[..., None] | None = None
    #: Scratch area a method may own. Defaults to a hidden directory
    #: under :attr:`out_dir`, which is what a command line wants: rebuilt
    #: every run, thrown away with the build directory.
    work_root: Path | None = None

    # -- local / remote -----------------------------------------------
    #: PEM of the user's MCUboot **public** key. Becomes
    #: ``keys/signing.pub`` in the build context, which is all of the key
    #: pair a build ever sees (ADR 0015 decision 8).
    signing_pub: str = ""

    # -- local / remote ------------------------------------------------
    #: Directories holding the hash-pinned MCUHome SDK package (ADR 0018).
    #: Read by **both** container-shaped methods, for the same reason and
    #: at the same moment: the resolved package hash is an input of the
    #: context ID, so the pin has to exist before a context does (E65).
    #: What differs afterwards is who fetches the bytes — this machine for
    #: ``local``, the build server out of its operator's own sources for
    #: ``remote``, which then verifies them against this pin.
    sdk_sources: Sequence[Path] = ()

    # -- local ---------------------------------------------------------
    #: Build-container reference to compile in; ``None`` takes the default.
    image: str | None = None
    #: Where the compiler cache lives on this machine. ``None`` takes the
    #: user's cache directory, which is what every build does unless
    #: somebody moved it — one cache per user, shared by every project.
    ccache_dir: Path | None = None

    # -- local-dev -----------------------------------------------------
    #: The **public** key file ``west`` compiles into the bootloader.
    bootloader_key: Path | None = None
    #: Where the MCUHome Zephyr module is installed, and where the caller
    #: was run — the two places the west workspace is looked for. The
    #: second is called ``started_in`` and not ``cwd`` on purpose: it is a
    #: directory the caller *states*, and naming it after the process's
    #: current one invites the next reader to fill it in by asking the
    #: process, which is exactly what ADR 0020 rules out
    #: (:mod:`mcuhome.model.userpaths`).
    module_dir: Path | None = None
    started_in: Path | None = None
    #: Zephyr snippets, per image.
    snippets: Sequence[str] = ()
    bootloader_snippets: Sequence[str] = ()
    #: Called once with the resolved west plan, before anything runs.
    on_plan: Callable[[Any], None] | None = None
    #: Where the compiler's own output goes; ``None`` inherits.
    stream: TextIO | None = None

    # -- remote --------------------------------------------------------
    #: The build server's address, as a person writes it: a host, a
    #: ``host:port``, or either with a scheme. :func:`websocket_url` turns
    #: it into the URL the socket needs. *Selecting* a server belongs to
    #: the caller — a configured builder or the fully manual
    #: ``--build-mode`` rung (ADR 0023);
    #: :func:`mcuhome.workbench.configuration.resolve_builder` is the
    #: configured path — and an address is what arrives here.
    server: str | None = None
    #: The bearer token for it, from the builder's
    #: ``secrets/build-server/<name>.yaml`` (or the manual rung's
    #: ``--build-token``). ``None`` sends no ``Authorization`` header at
    #: all, which this package permits because a third-party server may
    #: want none.
    token: str | None = None
    #: A build context directory to build instead of creating one. For a
    #: caller that already holds one — an embedder that assembled a
    #: context elsewhere, a build server that received one over a socket,
    #: a test driving a hand-written one. Left ``None``, which is the
    #: ordinary case, the build creates its own from :attr:`model` and
    #: :attr:`sdk_sources`. Either way it is a *base* context: locking it
    #: is the act of whoever builds it, and a client that sent one checks
    #: the identity the server answers with (E37).
    context_dir: Path | None = None
    #: Wait when the build server has no room. A busy server hands out a
    #: turn instead of a session, and waiting for it is what a person
    #: starting a build almost always wants; ``False`` is the caller that
    #: would rather be told now.
    wait_for_turn: bool = True
    #: How long that wait may last in total, in seconds. ``0`` removes
    #: the bound.
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS
    #: Called with a
    #: :class:`~mcuhome.workbench.sessionclient.SeatWait` each time a
    #: turn is refused, so a caller can say something true while nothing
    #: is happening. Deliberately **not** a step: the build has not
    #: started and may never start, and a step bar claiming otherwise
    #: would be showing progress that does not exist.
    on_wait: Callable[[Any], None] | None = None


@dataclass(frozen=True)
class BuildOutcome:
    """What a build method produced, in the one shape every method answers.

    :attr:`successful` is the only field a caller must consult before the
    others mean anything. :attr:`out_dir` is where the **unsigned**
    artifacts and the build report are, and :attr:`report` is that
    report's file name — the two together are what the one shared signing
    step needs, and they are the whole reason this class exists (E56).

    :attr:`artifacts` is the declared artifact set where the method
    declared one: ``local`` and ``remote`` read it out of a contract
    §5.4 result, in :class:`~mcuhome.model.artifacts.Artifact`, the same
    type on both sides. ``local-dev`` declares none — no build container
    ran, and what a host ``west`` build produced is described by its
    ``build-manifest.json`` instead — so it answers with an empty tuple
    rather than an invented list.
    """

    #: Which of :data:`METHODS` ran.
    method: str
    successful: bool
    #: The method's own word for the result: ``success``/``failure`` from
    #: a contract result document, ``success`` from a host build that did
    #: not raise.
    status: str
    #: The identity the work is attributed to, or ``""`` for ``local-dev``
    #: — a host build has no build context and therefore no context ID.
    context_id: str
    artifacts: tuple[Artifact, ...]
    #: Where the unsigned artifacts and the report are.
    out_dir: Path | None
    #: The build report's file name in :attr:`out_dir`: the contract's
    #: ``build-report.json`` for the two container-shaped methods, and
    #: ``build-manifest.json`` for ``local-dev``. Both carry the imgtool
    #: parameters the host signer needs (E55).
    report: str
    #: The build-container reference, where one was used.
    image: str = ""
    #: The method's own result object, untouched.
    detail: Any = None


def _compiler(module: str, method: str):
    """Import a compiler-side module, or refuse naming the distribution.

    The optional edge of ADR 0020 decision 3, resolved at call time. See
    the module docstring for why it is not an ``import`` statement.

    **Only the missing distribution is translated.** An ``ImportError``
    raised from *inside* an installed compiler — a broken ``zstandard``
    wheel, a C extension built for another interpreter — says nothing
    about ``mcuhome-compiler`` being absent, and answering it with "not
    installed, run pip install mcuhome-compiler" sends the reader to fix
    the one thing that is already right. So the refusal is made only when
    the failed import *is* ``mcuhome.compiler`` or something under it;
    anything else travels on untouched, with its own name in it.
    """
    import importlib

    name = f"mcuhome.compiler.{module}"
    try:
        return importlib.import_module(name)
    except ImportError as error:
        missing = error.name or ""
        if missing != "mcuhome.compiler" and not missing.startswith("mcuhome.compiler."):
            raise
        raise MethodUnavailable(
            f"The {method} build method needs mcuhome-compiler, and it is not installed.",
            hint=(
                "stages 4-5 — code generation and the compile itself — are their own "
                "distribution, so a workbench that only validates configurations or "
                "drives a build server does not carry them. Install it with:\n"
                "    pip install mcuhome-compiler"
            ),
        ) from error


def _work_root(request: BuildRequest, name: str) -> Path:
    return Path(request.work_root) if request.work_root else Path(request.out_dir) / name


def target_for_method(method: str | None, request: BuildRequest) -> BuildTarget:
    """The build target a method *name* and a request describe together.

    The bridge between the two vocabularies while both exist: a name is
    one word for two decisions, and a target states them apart
    (:mod:`mcuhome.workbench.buildtarget`). Everything method-specific on
    :class:`BuildRequest` is read **here and nowhere else**, so that the
    day those fields move onto the targets there is one call site to
    change rather than three build methods.

    *method* goes through :func:`resolve_method` first, so ``None`` and
    the empty string mean the default and an unknown name is the same
    refusal a caller would have got from ``run_build``.
    """
    chosen = resolve_method(method)
    if chosen == LOCAL:
        return LocalBuild(
            execution=ContainerExecution(image=request.image, ccache_dir=request.ccache_dir)
        )
    if chosen == LOCAL_DEV:
        return LocalBuild(
            execution=WorkspaceExecution(
                bootloader_key=request.bootloader_key,
                module_dir=request.module_dir,
                started_in=request.started_in,
                snippets=tuple(request.snippets),
                bootloader_snippets=tuple(request.bootloader_snippets),
                on_plan=request.on_plan,
                stream=request.stream,
            )
        )
    return RemoteBuild(
        server=request.server,
        token=request.token,
        wait=request.wait_for_turn,
        max_wait_seconds=request.max_wait_seconds,
    )


async def build_firmware(request: BuildRequest, *, target: BuildTarget) -> BuildOutcome:
    """Build *request* at *target*, and answer in the one outcome shape.

    The seam. Above it a caller decides *what* to build (a device model,
    or a build context already created from one) and *where*; below it
    the three compositions differ in everything and agree on the answer.
    It is also where a build server enters: what reaches it over a socket
    is a context and a target of its own making, and from that point on
    the work is the same work a local build does.

    Raises whatever typed refusal the target's composition raises — a
    missing build container, a missing SDK package, a build server that
    said no. A build that ran and *failed* is not an exception: it comes
    back with :attr:`BuildOutcome.successful` false and the composition's
    own account in :attr:`BuildOutcome.detail`, because a failed compile
    is an answer and a caller usually wants to render it rather than
    catch it. A target this package does not implement is a
    :class:`TypeError` rather than a refusal: a name can be mistyped, an
    object cannot.

    The build directory is held for the duration (:mod:`…buildlock`), so
    a second build of it refuses in words instead of deleting this one's
    work under it. Here rather than in a composition, because every one
    of them writes into the same directory and the collision does not
    care which two were running — nor whether the other one is a command
    line or a dashboard.
    """
    with build_lock(request.out_dir, device=request.model.device.name):
        if isinstance(target, LocalBuild):
            execution = target.execution
            if isinstance(execution, ContainerExecution):
                return await _run_local(request, execution)
            if isinstance(execution, WorkspaceExecution):
                return await _run_local_dev(request, execution)
            raise TypeError(
                f"{type(execution).__name__} is not a build execution this package runs"
            )
        if isinstance(target, RemoteBuild):
            return await _run_remote(request, target)
        raise TypeError(f"{type(target).__name__} is not a build target this package runs")


async def run_build(request: BuildRequest, *, method: str = DEFAULT_METHOD) -> BuildOutcome:
    """Run *method* over *request*: :func:`build_firmware` by method name.

    The name-shaped entry point, kept for callers that select a build
    method from a command-line flag or a configuration value and have
    nothing to say about the two axes apart. It resolves the name to a
    target (:func:`target_for_method`) and hands over; everything the
    seam documents holds here unchanged, including
    :class:`UnknownMethod` for a name that is not one of
    :data:`METHODS`.
    """
    return await build_firmware(request, target=target_for_method(method, request))


def compose_local_build(
    model: DeviceModel,
    *,
    signing_pub: str,
    sdk_sources: Sequence[Path],
    work_root: Path,
    env: dict[str, str],
    image: str | None = None,
    jobs: int = 1,
    mode: str = "clean",
    ccache_dir: Path | None = None,
    created: datetime | None = None,
    context_dir: Path | None = None,
    on_line: Any = None,
    on_step: Any = None,
    docker: Any = None,
):
    """The container execution's composition: resolve, create, lock, drive.

    The two roles E61 separates, composed where the client half lives:
    the workbench states the requirement — it creates and locks the
    context, recording the image the compiler half answered with — and
    the compiler's backend half does the answering
    (:func:`~mcuhome.compiler.localbuild.resolve_checked_image`) and the
    driving (:func:`~mcuhome.compiler.localbuild.run_locked_build`).
    Order is the composition's promise: the image refusals cost no
    context directory and no SDK lookup. Synchronous — ``build_firmware``
    offloads it; *docker* is the test seam, threaded through to both
    compiler halves.

    *context_dir* is the caller that already holds a **base** context and
    wants this one built — an embedder that assembled one elsewhere, a
    build server that received one over a socket. It is used as it is:
    nothing is resolved, nothing is written into it but the lock, and no
    context step is announced, because this composition did not create
    one. Left ``None``, which is the ordinary case, the context is
    created at ``<work root>/context`` — the same call the ``remote``
    method makes (E65), because what a context is does not depend on
    where it is built.
    """
    localbuild = _compiler("localbuild", LOCAL)
    sources = tuple(Path(source) for source in sdk_sources)
    resolved = localbuild.resolve_checked_image(
        image, line=model.toolchain.zephyr_line, env=dict(env), docker=docker
    )
    work_root = Path(work_root)
    supplied = context_dir is not None
    context_dir = Path(context_dir) if supplied else work_root / "context"
    if not supplied:
        if on_step is not None:
            on_step("context")
        create_build_context(
            model,
            out_dir=context_dir,
            sdk_sources=sources,
            signing_pub=signing_pub,
            created=created or datetime.now(UTC),
        )
    lock_context(
        context_dir,
        # The backend half's answer, recorded verbatim — including a
        # `None` digest for an image that was never pushed, which says
        # "these bytes are not fetchable anywhere" rather than inventing
        # a digest that looks as if they were.
        container=ContainerResolution.from_reference(resolved.reference, digest=resolved.digest),
    )
    if on_step is not None:
        if not supplied:
            # What the context turned out to be, read back off the locked
            # directory: the step announced itself before any of this was
            # decided, and the decisions are the interesting part.
            on_step("context", **context_facts(context_dir))
        on_step("compile", image=resolved.reference, digest=resolved.digest, jobs=jobs)
    return localbuild.run_locked_build(
        context_dir,
        image=resolved.reference,
        sdk_sources=sources,
        work_root=work_root / "backend",
        env=dict(env),
        jobs=jobs,
        mode=mode,
        ccache_dir=ccache_dir,
        on_line=on_line,
        docker=docker,
    )


async def _run_local(request: BuildRequest, execution: ContainerExecution) -> BuildOutcome:
    """Local, in a container: :func:`compose_local_build`, offloaded.

    Synchronous underneath — it drives ``docker`` with a subprocess per
    invocation — so it is offloaded rather than awaited. The composition
    is looked up as a module global so that a caller (or a test) that
    replaced ``compose_local_build`` is the one that runs.
    """
    result = await asyncio.to_thread(
        compose_local_build,
        request.model,
        signing_pub=request.signing_pub,
        sdk_sources=tuple(Path(source) for source in request.sdk_sources),
        work_root=_work_root(request, ".mcuhome-local"),
        env=dict(request.env),
        image=execution.image,
        jobs=request.jobs,
        mode=request.mode,
        ccache_dir=execution.ccache_dir,
        context_dir=request.context_dir,
        on_line=request.on_line,
        on_step=request.on_step,
    )
    outcome = result.outcome
    return BuildOutcome(
        method=LOCAL,
        successful=outcome.successful,
        status=outcome.status,
        context_id=outcome.context_id,
        artifacts=tuple(outcome.artifacts),
        out_dir=result.out_dir,
        report=BUILD_REPORT_FILE,
        image=result.image,
        detail=result,
    )


async def _run_local_dev(request: BuildRequest, execution: WorkspaceExecution) -> BuildOutcome:
    """Local, in the caller's workspace: :func:`…devbuild.run_dev_build`.

    Stage 4 has already run — the generated application is in *out_dir* —
    because a ``local-dev`` caller wants that tree whether or not it goes
    on to compile, and ``--generate-only`` is exactly the caller that
    stops there.
    """
    devbuild = _compiler("devbuild", LOCAL_DEV)
    if (
        execution.bootloader_key is None
        or execution.module_dir is None
        or execution.started_in is None
    ):
        raise BuildError(
            f"The {LOCAL_DEV} build method needs a public key file, a module "
            "directory and a working directory, and this request names none.",
            hint=(
                "the key is the public half MCUboot verifies against; the two "
                "directories are where the west workspace is looked for, and only "
                "the caller knows either of them"
            ),
        )

    def announce_and_step(plan: Any) -> None:
        # The plan callback fires after every pre-flight refusal and
        # immediately before west runs — which makes it exactly the
        # moment the build enters its compile step, so the method emits
        # the step itself rather than leaving a caller to guess it.
        if execution.on_plan is not None:
            execution.on_plan(plan)
        if request.on_step is not None:
            request.on_step("compile")

    result = await asyncio.to_thread(
        devbuild.run_dev_build,
        request.model,
        out_dir=Path(request.out_dir),
        bootloader_key=Path(execution.bootloader_key),
        module_dir=Path(execution.module_dir),
        cwd=Path(execution.started_in),
        env=dict(request.env),
        jobs=request.jobs,
        snippets=tuple(execution.snippets),
        bootloader_snippets=tuple(execution.bootloader_snippets),
        on_plan=announce_and_step,
        stream=execution.stream,
    )
    return BuildOutcome(
        method=LOCAL_DEV,
        successful=True,
        status="success",
        context_id="",
        artifacts=(),
        out_dir=Path(request.out_dir),
        report=MANIFEST_FILE,
        detail=result,
    )


def _remote_context(request: BuildRequest, work_root: Path) -> Path:
    """The base context a remote build sends, at ``<work root>/context``.

    Placed exactly where the ``local`` method places its own and written
    by the same function, so the two methods differ in where the context
    goes and in nothing about what it is.
    """
    context_dir = Path(work_root) / "context"
    create_build_context(
        request.model,
        out_dir=context_dir,
        sdk_sources=tuple(Path(source) for source in request.sdk_sources),
        signing_pub=request.signing_pub,
    )
    return context_dir


async def _run_remote(request: BuildRequest, target: RemoteBuild) -> BuildOutcome:
    """The build-server method: :func:`…sessionclient.run_remote_build`.

    The session client is imported here rather than at module level so
    that importing this package costs neither its weight nor the
    ``remote`` extra: ``aiohttp`` and ``zstandard`` are refused in words
    the first time a frame would be sent, not at import.

    Two things are refused before that, because neither can be invented.
    **The server address** is E53's ladder and its rungs belong to the
    caller — a build server has no default, and a wrong one is a build
    context sent to a stranger. **An SDK source** is E65's: the context
    this method creates carries the SDK package's version *and* its
    sha256, and both come from the client's own source directories
    (later, from a registry index). The version is the key the server
    resolves the package by; the hash is what it verifies the bytes it
    found against, which is what makes "same version, other bytes" a
    typed refusal there instead of a silent build against another SDK.
    A client that states no source can resolve neither, and a context
    pinned to whatever the server happened to have would be an identity
    that describes nothing.

    With both in hand this is the ``local`` method's own composition with
    a socket in place of a container: resolve the pin, write the base
    context through the seam both methods share
    (:func:`~mcuhome.workbench.contextdir.create_build_context`), and
    hand the directory to the session client. It is the **base** context
    that goes — unlocked, without a ``manifest.yaml`` — because freezing
    it is the server's act (ADR 0019, E7) and the client's duty is to
    compare the identity it answers with (E37). An embedder that already
    holds a context passes it as :attr:`BuildRequest.context_dir` and
    none of this runs.
    """
    if not target.server:
        raise RemoteNotConfigured(
            "The remote build method needs the address of a build server, and none is set.",
            hint=(
                "configure a builder once, or name the server outright:\n"
                "    builders:                    # mcuhome.yaml, or your user/system\n"
                "      - name: attic              # configuration.yaml\n"
                "        type: remote\n"
                "        server: <host[:port]>\n"
                "    with its token in secrets/build-server/attic.yaml, selected via\n"
                "    --builder attic or once via default_builder;\n"
                "or fully manually:\n"
                "    --build-mode remote --build-server <host[:port]> [--build-token <token>]\n"
                "A build server is not discovered and has no default: the build "
                "context carries the device model, so the address is a decision "
                "rather than a lookup."
            ),
        )
    url = websocket_url(target.server)
    work_root = _work_root(request, ".mcuhome-remote")
    context_dir = Path(request.context_dir) if request.context_dir is not None else None
    if context_dir is None:
        if not request.sdk_sources:
            raise RemoteNotConfigured(
                "The remote build method needs an SDK source to pin the build context "
                "with, and none is configured.",
                hint=(
                    "point at a directory holding an MCUHome SDK package:\n"
                    "    mcuhome config set sdk_sources <dir> --user\n"
                    "or pass --sdk-sources <dir> for a single build."
                ),
            )
        if request.on_step is not None:
            request.on_step("context")
        # Off the event loop: this hashes nothing large, but it reads an
        # index, writes the model and the key, and copies the patch set —
        # filesystem work with no await in it, in a method whose whole
        # point is that a caller's loop keeps running while it waits.
        context_dir = await asyncio.to_thread(_remote_context, request, work_root)
        if request.on_step is not None:
            # No `id` among these: freezing the context is the server's
            # act (E37), so a base context on its way out has none yet.
            request.on_step("context", **context_facts(context_dir))

    from mcuhome.workbench import sessionclient

    if request.on_step is not None:
        request.on_step("compile", server=target.server)
    result = await sessionclient.run_remote_build(
        context_dir,
        url=url,
        token=target.token,
        work_root=work_root,
        mode=request.mode,
        on_line=request.on_line,
        on_wait=request.on_wait,
        wait=target.wait,
        max_wait=target.max_wait_seconds,
    )
    return BuildOutcome(
        method=REMOTE,
        successful=result.successful,
        status=result.status,
        context_id=result.context_id,
        artifacts=tuple(result.artifacts),
        out_dir=result.out,
        report=BUILD_REPORT_FILE,
        detail=result,
    )
