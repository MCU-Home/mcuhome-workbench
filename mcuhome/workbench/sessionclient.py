# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``remote`` build method: the session-protocol client (E18, ADR 0019).

The third build method's whole client half. ``local-dev`` runs a build in
the developer's own workspace and ``local`` drives a build container
through the invocation ABI on this machine
(:mod:`mcuhome.compiler.localbackend`); ``remote`` drives *a build server*
over one WebSocket, which speaks the eleven verbs of ADR 0019's session
protocol. This module is that conversation, and nothing else: it uploads
a build context, freezes it, starts an invocation, follows its events and
takes the artifacts back.

**The build always returns an unsigned image** (E55, E56). Signing is a
single host-side step that happens after the artifacts are back, over the
``build-report.json`` the build container declares (contract §7.2.1), and
it is the same step for all three methods. Which leads to the one
invariant this module is built around:

    **The private signing key never leaves the local machine.** It is a
    purely client-side secret, so it appears in no frame this client
    sends — not as a value, not as a path, not as a configuration slot.
    The API below therefore has no parameter that could carry one: the
    only key a build ever sees is ``keys/signing.pub``, which is context
    content the caller already placed (ADR 0015 decision 8, ADR 0018's
    2026-08-09 amendment). A key that cannot be passed cannot be sent.

**Shape, so slice 4 can dispatch uniformly.** :func:`run_remote_build`
mirrors :meth:`mcuhome.compiler.localbackend.LocalBackend.run`: a locked-
able context directory and a work root go in, an unsigned artifact set
plus the build report come out, progress arrives through a line sink.
Turning a device model into a context directory is the shared step above
both of them and deliberately not repeated here.

**Layering.** This module is in the workbench and must not import the
compiler (ADR 0020 decision 3), so the two disciplines it shares with
:mod:`mcuhome.compiler.localbackend` — safe extraction and strict
containment — are re-stated here rather than imported. Both copies name
each other; the rule they implement is build-container contract §9.1,
which states it once for "whatever transport delivered" an input.

**Optional dependencies.** ``aiohttp`` and ``zstandard`` are the
transport and the archive codec, and neither is a base dependency of
``mcuhome-workbench``: a workbench that only validates a configuration or
builds locally should not have to carry an HTTP stack. They are declared
as the ``remote`` extra and imported lazily, so a caller that never asks
for a remote build never pays for them and one that does gets a refusal
naming the install rather than an ``ImportError``.

Three things this client owes the protocol that the protocol does not
enforce:

* **One announce-then-stream sequence at a time** (E41, E45). An
  announcement and the BINARY frames that follow it are correlated by
  order and by nothing else, in both directions, so two uploads or two
  downloads interleaved on one socket are two byte streams nobody can
  tell apart afterwards. The build server holds that discipline on its
  side with a per-connection lock; :class:`SessionClient` holds the
  mirror of it (:attr:`SessionClient._sequence`), which is what makes
  concurrent calls on one client safe rather than forbidden.
* **The context-ID comparison is the client's duty** (E37). The product
  owner chose the minimal wire shape for ``lock-context``: the request
  carries the session id, the answer carries the context ID, and the
  server never sees the client's own value — so it can never raise that
  mismatch. :meth:`SessionClient.lock_context` computes the ID from the
  bytes it sent, compares, and **closes the session** on a disagreement.
  ADR 0019's "observable moment at which both sides compare values they
  computed independently" exists only because this method does that.
* **Per-file integrity of a download is checked here** (E45). The
  ``get-artifact`` announcement carries the *archive's* hash, computed at
  egress; the per-file hashes are the ones the client already holds from
  the ``invocation.verdict`` frame, and checking them is what keeps
  the announced hash a transport check rather than a second integrity
  claim.

**The ingress caps are announced, and this client sizes itself by them**
(E44, E57). ``capabilities`` carries an ``ingress`` block with the five
caps of ADR 0019 decision 8 — read from the server's own configuration,
so an operator who lowered one has lowered what a client is told — plus
``frame_bytes``, the largest WebSocket message the endpoint accepts. The
last one is announced because it is the only bound whose overrun is a
dropped connection rather than a typed refusal: it cannot be discovered
by hitting it and surviving. :meth:`Capabilities.ingress` reads all six
and falls back to E44's decided defaults for anything a peer leaves out
— never to a number of this client's own invention.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import stat
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcuhome.model.artifacts import Artifact, artifacts_from_wire
from mcuhome.model.context import (
    BACKEND_DIR,
    CONTEXT_FILE,
    CONTEXT_VERSION,
    MANIFEST_FILE,
    ContextFile,
    context_id,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file

from mcuhome.workbench.contextdir import read_context_request

__all__ = [
    "DEFAULT_CALL_TIMEOUT",
    "DEFAULT_CHUNK_BYTES",
    "E44_CAPS",
    "MAX_ARTIFACT_ARCHIVE_BYTES",
    "MAX_ARTIFACT_BYTES",
    "MAX_INBOUND_FRAME_BYTES",
    "SESSION_PROTOCOL_VERSION",
    "ZSTD_LEVEL",
    "ArtifactDelivery",
    "Capabilities",
    "ContextIdMismatch",
    "ContextTooLarge",
    "EventSink",
    "IngressCaps",
    "IngressSpent",
    "LineSink",
    "PackedContext",
    "PrivateKeyRefused",
    "RemoteBuildResult",
    "RemoteDependencyMissing",
    "RemoteError",
    "RemoteTransportError",
    "ServerRefusal",
    "SessionClient",
    "SessionPoisoned",
    "pack_context",
    "run_remote_build",
]

#: Where a caller's own callback is reported when it raises. A sink is a
#: consumer, never the transport, so its failure is news about the caller
#: and must not be mistaken for a dead socket (see :meth:`SessionClient._deliver`).
_logger = logging.getLogger(__name__)

#: The session protocol version this client speaks. A mismatch is a typed
#: rejection at the door (``version.protocol-mismatch``) and never a
#: downstream failure, which is why the number is stated in
#: ``open-session`` rather than discovered.
SESSION_PROTOCOL_VERSION = 2

#: How many bytes of an archive go into one outbound BINARY frame, absent
#: a smaller announced limit. A quarter of a megabyte, which is the same
#: number the build server streams *downloads* at and well under the
#: 8 MiB ``max_msg_size`` its ``/ws`` endpoint accepts and announces
#: (E57). It stays this client's own conservative choice rather than
#: knowledge of the peer: a peer that announces a larger frame has given
#: a licence and not an instruction, and one that announces none — an
#: older or third-party server — is a peer nothing is known about.
DEFAULT_CHUNK_BYTES = 256 * 1024

#: The largest inbound WebSocket message this client will assemble, and
#: the one limit that can refuse a message *before* its bytes have been
#: buffered — aiohttp rejects an oversized frame at the reader, in the
#: same place the build server's endpoint does. Passing 0 there would
#: disable a limit aiohttp applies by default, on a socket whose peer
#: this module elsewhere calls "the least trusted component in the
#: system". The number is the 8 MiB the server's own ``/ws`` endpoint
#: accepts, which is far above anything it sends: log lines are capped at
#: 8 KiB, download chunks at 256 KiB and every other frame is small JSON.
MAX_INBOUND_FRAME_BYTES = 8 * 1024 * 1024

#: What one artifact delivery may cost this machine. E44's caps size what
#: goes *up*; nothing announces a ceiling for what comes down, so these
#: are this client's own — an artifact archive above the first number is
#: refused from its announcement, before a byte reaches the spool, and an
#: archive that expands past the second is refused mid-stream. A firmware
#: set is a few megabytes; both numbers are orders above that and exist
#: so that a hostile or broken server cannot fill a developer's disk with
#: a decompression bomb.
MAX_ARTIFACT_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024

#: Fixed compression level, and one of the two things that make two packs
#: of one context byte-identical (``scripts/build_sdk_archive.py`` states
#: the same discipline for the SDK package). The archive bytes are never
#: hashed into any identity — the context ID is computed over the *files*
#: — so this is reproducibility for a human comparing two uploads, not a
#: correctness requirement.
ZSTD_LEVEL = 19

#: How much of a file is read at a time. Bounded so that neither packing
#: nor unpacking a context has to hold one in memory.
_BLOCK = 1 << 20

#: How long a client waits for one command's answer, in seconds. Long,
#: because ``send-context`` is answered only once the whole archive has
#: been received, hashed and unpacked; bounded, because a server that
#: stopped answering must not leave a caller hanging forever. A ``build``
#: is *not* bounded by this: the verb answers immediately with an
#: invocation id and the completion arrives as an event (E46).
DEFAULT_CALL_TIMEOUT = 300.0


# --------------------------------------------------------------------------
# The exception family
# --------------------------------------------------------------------------


class RemoteError(BuildError):
    """Anything this client refuses or is refused. A ``BuildError``, so a
    command line renders it the way it renders every other refusal."""


class RemoteDependencyMissing(RemoteError):
    """``aiohttp`` or ``zstandard`` is not installed.

    Worded the way :mod:`mcuhome.compiler.localbuild` words a missing
    docker: state the fact, then name the exact fix.
    """


class RemoteTransportError(RemoteError):
    """The connection died, a frame was unreadable, or an answer never came.

    Below the session protocol's own vocabulary: a frame that never
    parsed has no session and no verb to attribute a refusal to, which is
    the same reason the server keeps a pre-registry ``bad_request`` code.
    """


class ServerRefusal(RemoteError):
    """A typed refusal from the build server, with its whole envelope.

    ``retryable`` is **the server's promise and never this client's
    guess** — the registry on the far side owns it. A code this client
    does not know is treated as non-retryable fatal and its message is
    surfaced, which is exactly what lets that registry grow without
    breaking anyone.
    """

    def __init__(self, error: dict[str, Any], *, verb: str) -> None:
        code = str(error.get("code") or "unknown")
        message = str(error.get("message") or "The build server refused this command.")
        super().__init__(
            f"The build server refused {verb}: {message}",
            hint=f"the server answered {code}; its details are on the exception",
        )
        #: The dotted registry code, or ``bad_request``/``internal_error``
        #: for a frame that never reached the registry.
        self.code = code
        self.layer = error.get("layer") or code.partition(".")[0]
        #: ``None`` when the envelope carried none — a pre-registry error
        #: makes no retryability promise, and inventing one here would
        #: forge exactly the promise the registry exists to own.
        self.retryable = error.get("retryable")
        self.details = error.get("details") or {}
        self.verb = verb
        self.server_message = message


class SessionPoisoned(ServerRefusal):
    """``session.poisoned`` — terminal for the session (E39).

    An interrupted patch application left trees no future build may
    trust. ``get-artifact`` and ``close-session`` still work, which is the
    point: the moment a session poisons is the moment its owner most
    wants the logs. Every *working* command from here on is refused, so
    this client marks the session terminal and does not retry into it.
    """


class ContextIdMismatch(RemoteError):
    """E37: the server's context ID is not the one the client computed.

    Terminal, and the session is closed before this is raised. The two
    values are on the exception because the whole worth of the freeze is
    that both sides computed one independently — a message that named
    only one of them would hide the half that matters.
    """

    def __init__(self, *, local: str, remote: str) -> None:
        super().__init__(
            "The build server froze this context under a different identity than the "
            f"bytes sent to it: the server answered {remote}, the same files hash to "
            f"{local} here.",
            hint=(
                "the context ID is content-addressed and both sides compute it from the "
                "same frozen rule (build-container contract §3.3), so a disagreement "
                "means the context on the server is not the context that was sent — the "
                "session has been closed; open a new one and send it again"
            ),
        )
        self.local_id = local
        self.remote_id = remote


class ContextTooLarge(RemoteError):
    """A cap would be exceeded, refused here rather than mid-upload.

    The server enforces its caps while the bytes arrive and answers
    ``policy.ingress-limit-exceeded``; this refuses the same thing before
    a single frame goes out, which is the difference between "your
    context is too big" and "your context was too big after 60 MiB of
    upload".
    """


class PrivateKeyRefused(RemoteError):
    """A context member holds private key material, so nothing is sent.

    The module's crown invariant is that the private signing key never
    leaves this machine, and the API has no slot one could be passed in.
    A key does not need a slot to travel, though: a file dropped into the
    context directory would be packed as ordinary content, which is why
    the packer looks and this refusal exists. Loud on purpose — a stray
    key is a mistake worth stopping a build for, and the fix (move it out
    of the context; only ``keys/signing.pub`` belongs there) is short.
    """


# --------------------------------------------------------------------------
# The optional dependencies
# --------------------------------------------------------------------------


def _require(module: str, what: str):
    """Import an optional dependency, or refuse naming the install."""
    try:
        return __import__(module)
    except ImportError as error:  # pragma: no cover - exercised by the extras test
        raise RemoteDependencyMissing(
            f"The remote build method needs {module}, and it is not installed ({what}).",
            hint=(
                "the WebSocket transport and the tar.zst archive codec are an optional "
                "extra of this package, so a workbench that never builds remotely does "
                "not carry them — install it with:\n"
                "    pip install 'mcuhome-workbench[remote]'"
            ),
        ) from error


def _aiohttp():
    return _require("aiohttp", "the session protocol runs over one WebSocket")


def _zstandard():
    return _require("zstandard", "a build context and its artifacts travel as tar.zst")


# --------------------------------------------------------------------------
# Capabilities and the caps
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IngressCaps:
    """What one upload may cost, as the client applies it before sending.

    **These numbers belong to the server** (E44: "the config is the
    policy"), and this client uses an announced value wherever it finds
    one. The build server announces all six in ``capabilities`` (E57),
    out of its own configuration. Where a peer announces none — an older
    or a third-party server — E44's decided defaults stand
    (:data:`E44_CAPS`), because a client that invented a cap of its own
    would refuse contexts a server would have taken.

    :attr:`frame_bytes` is not one of E44's five. It is the largest
    single WebSocket message the peer accepts, which bounds the *chunk*
    an archive is streamed in rather than the archive — and it is the one
    limit whose overrun is a dropped connection instead of a typed
    refusal, which is why E57 made it part of the announcement beside the
    caps. A peer that states none leaves this client's conservative
    :data:`DEFAULT_CHUNK_BYTES` in place.
    """

    compressed_bytes: int
    decompressed_bytes: int
    entries: int
    file_bytes: int
    path_depth: int
    frame_bytes: int = DEFAULT_CHUNK_BYTES

    #: The names read out of an announcement, in the order they are
    #: tried. **This is the wire shape** since E57: the product owner
    #: fixed the ``capabilities`` payload's ``ingress`` block on the
    #: names this client had guessed, so one server changed and nothing
    #: here did. A peer that announces other names, or none, simply
    #: leaves the defaults in place.
    _FIELDS = (
        "compressed_bytes",
        "decompressed_bytes",
        "entries",
        "file_bytes",
        "path_depth",
        "frame_bytes",
    )

    @staticmethod
    def announced(payload: dict[str, Any]) -> IngressCaps:
        """The caps a ``capabilities`` payload states, over E44's defaults."""
        found = payload.get("ingress")
        if not isinstance(found, dict):
            return E44_CAPS
        values = {}
        for name in IngressCaps._FIELDS:
            number = found.get(name)
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                continue
            values[name] = number
        return E44_CAPS if not values else IngressCaps(**{**E44_CAPS.as_dict(), **values})

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in IngressCaps._FIELDS}


@dataclass(frozen=True)
class IngressSpent:
    """What one session has already spent of its ingress budget.

    E44's caps are **cumulative across the base context and every
    extension of one session** — that is what the build server's ledger
    charges, and a client that checked each archive in isolation would
    pass its own check and then be refused at
    ``policy.ingress-limit-exceeded`` after the whole upload had arrived,
    which is precisely what :class:`ContextTooLarge` exists to avoid.

    A conservative lower bound and never an accounting of the server's
    own: the server also charges directory members and archives it
    refused, so this can only ever be *less* than what the far side has
    booked. That is the safe direction — it refuses what is certainly too
    much and never assumes acceptance of what is not.
    """

    entries: int = 0
    decompressed_bytes: int = 0
    compressed_bytes: int = 0

    def plus(self, packed: PackedContext) -> IngressSpent:
        """This ledger with *packed* charged to it."""
        return IngressSpent(
            entries=self.entries + len(packed.members),
            decompressed_bytes=self.decompressed_bytes + packed.decompressed,
            compressed_bytes=self.compressed_bytes + packed.size,
        )


#: Nothing spent yet — the ledger a fresh session starts from, and what
#: :func:`pack_context` assumes for a caller packing one archive with no
#: session behind it.
_NOTHING_SPENT = IngressSpent()

#: E44's decided defaults: 64 MiB compressed, 256 MiB decompressed
#: cumulative, 4096 entries, 64 MiB per file, path depth 16. They are the
#: build server's own defaults, so a client that has heard no
#: announcement and applies these refuses exactly what an unconfigured
#: server would refuse.
E44_CAPS = IngressCaps(
    compressed_bytes=64 * 1024 * 1024,
    decompressed_bytes=256 * 1024 * 1024,
    entries=4096,
    file_bytes=64 * 1024 * 1024,
    path_depth=16,
)


@dataclass(frozen=True)
class Capabilities:
    """The pre-session answer: what this server has, before anything is sent.

    The one verb that carries no session id, because there is no session
    yet to carry. It is what lets a workbench choose a build container
    during pin resolution rather than discover a mismatch from inside
    one, and — with :meth:`ingress` — what sizes the upload.
    """

    payload: dict[str, Any]

    @property
    def protocol_version(self) -> Any:
        return self.payload.get("protocol", {}).get("version")

    @property
    def context_format(self) -> dict[str, Any]:
        return self.payload.get("protocol", {}).get("context_format", {})

    @property
    def profiles(self) -> tuple[str, ...]:
        return tuple(self.payload.get("protocol", {}).get("profiles", ()))

    @property
    def containers(self) -> tuple[dict[str, Any], ...]:
        """Every build-container image this server can serve, with its digest."""
        found = self.payload.get("containers")
        return tuple(found) if isinstance(found, list) else ()

    @property
    def patch_policy(self) -> dict[str, Any]:
        found = self.payload.get("patch_policy")
        return found if isinstance(found, dict) else {}

    def allows_patch_layer(self, layer: str) -> bool:
        """Whether a context may patch *layer* here. Deny by default."""
        entry = self.patch_policy.get(layer)
        return bool(entry.get("allow")) if isinstance(entry, dict) else False

    def ingress(self) -> IngressCaps:
        return IngressCaps.announced(self.payload)


# --------------------------------------------------------------------------
# Packing a context (E41) and its extensions (E42)
# --------------------------------------------------------------------------

#: What is never in a context archive. ``manifest.yaml`` is written by the
#: locking party and "is never an extraction target"; ``.mcuhome/`` is
#: backend plumbing that a context does not carry at all.
_NEVER_PACKED = frozenset({MANIFEST_FILE})


@dataclass(frozen=True)
class PackedContext:
    """One tar.zst on disk, plus what the client must remember about it.

    :attr:`files` is the integrity list of exactly what went into the
    archive — the E37 comparison is computed from it, which is what makes
    "the ID of the bytes I sent" a statement about bytes rather than
    about a directory somebody may have edited since.

    :attr:`members` is the *archive's* member list, which is a different
    statement and deliberately so: ``context.yaml`` is an archive member
    of a base context and is never in the integrity list, so a guard that
    asks "did this archive carry the pin file?" has to read this one.
    :attr:`decompressed` is the plain tar's size, i.e. what the server
    charges its decompressed ledger.
    """

    path: Path
    size: int
    sha256: str
    files: tuple[ContextFile, ...]
    members: tuple[str, ...] = ()
    decompressed: int = 0

    def announcement(self) -> dict[str, Any]:
        return {"size": self.size, "sha256": self.sha256}


def _named_member(entry: str) -> str:
    """One caller-named context-relative path, or a refusal.

    The outbound twin of :func:`_safe_member_name`, and the reason it
    exists at all: ``only`` is caller data, a path is not a value the
    module's "a key that cannot be passed cannot be sent" invariant
    covers, and normalizing ``..`` away would silently pack whatever it
    pointed at. Refused rather than repaired, by the same rule the build
    server applies to a member name it receives.
    """
    cleaned = entry.replace(os.sep, "/").strip("/")
    usable = (
        cleaned
        and "\\" not in cleaned
        and "\x00" not in cleaned
        and not entry.startswith("/")
        and all(part not in ("", ".", "..") for part in cleaned.split("/"))
    )
    if not usable:
        raise RemoteError(
            f"{entry!r} is not a usable context path, so nothing was packed.",
            hint=(
                "the paths of an extension are relative to the context directory, use "
                "forward slashes and carry no empty, . or .. segment — anything else "
                "names a file outside the context, which is not this client's to send"
            ),
        )
    return cleaned


def _members(
    root: Path, *, only: Sequence[str] | None = None, exclude: frozenset[str] = frozenset()
) -> tuple[str, ...]:
    """The context-relative paths to pack, deterministically ordered.

    Sorted by path so that member order is a property of the context
    rather than of the filesystem that produced it — the same rule
    ``scripts/build_sdk_archive.py`` follows, and the same rule the
    context ID's ``files`` list follows, so the two orders agree by
    construction rather than by coincidence.

    Every entry of *only* is checked for containment as well as for
    shape: a caller-named path is the one path in this module that does
    not come from walking *root*, so it is the one that could leave it.
    """
    if only is not None:
        chosen = []
        for entry in only:
            name = _named_member(entry)
            if _contained(root, name) is None:
                raise RemoteError(
                    f'"{name}" does not stay inside the context directory, so nothing was packed.',
                    hint=(
                        "a segment of the path is a symlink or is not a directory — a "
                        "context carries what is under its own root and nothing else"
                    ),
                )
            chosen.append(name)
    else:
        chosen = [
            path.relative_to(root).as_posix() for path in sorted(root.rglob("*")) if path.is_file()
        ]
    kept = [
        path
        for path in chosen
        if path not in _NEVER_PACKED
        and path not in exclude
        and path.split("/", 1)[0] != BACKEND_DIR
    ]
    return tuple(sorted(dict.fromkeys(kept)))


def pack_context(
    root: Path,
    *,
    spool: Path,
    caps: IngressCaps = E44_CAPS,
    only: Sequence[str] | None = None,
    for_extension: bool = False,
    spent: IngressSpent = _NOTHING_SPENT,
) -> PackedContext:
    """Pack *root* into a deterministic ``tar.zst`` at *spool*.

    The wire format of E41, and the only one: tar.zst in both directions,
    chosen for family consistency with the SDK package the same contract
    pins. There is no format negotiation, so there is no format field.

    Determinism is the discipline of ``scripts/build_sdk_archive.py``,
    for the same reason and by the same means: PAX format, entries sorted
    by name, one fixed mtime, ``uid``/``gid`` 0 with empty
    ``uname``/``gname``, modes narrowed to 0644, one fixed zstd level and
    a single-threaded compressor. The mode bits are not context content —
    nothing in the format reads one, and the server discards them on the
    way in — so narrowing them removes a source of noise rather than
    information.

    Streaming throughout: the tar is written to a sibling spool and then
    compressed out of it, so neither half is ever held in memory. *caps*
    is applied **before** the first frame goes out; the server enforces
    the same numbers while the bytes arrive, and being refused here is
    the cheaper half of the same answer.

    *only* packs exactly the named context-relative paths — that is
    ``extend-context``'s add/overwrite half (E42). Left ``None``, the
    whole context is packed, which is ``send-context``'s.

    *for_extension* excludes ``context.yaml`` from the **archive**, not
    merely from the integrity list: it carries the pins the session was
    admitted on, an extension carrying it is refused with
    ``context.pins-immutable``, and that refusal would arrive after the
    whole upload. Excluding it here is what makes
    :meth:`SessionClient.extend_context` over a whole directory the legal
    call the verb's shape says it is.

    *spent* is what this session has already sent (:class:`IngressSpent`).
    The caps are cumulative on the server's side, so each of the three
    checks below compares *this archive plus everything before it*
    against the cap rather than this archive alone.
    """
    zstandard = _zstandard()
    root = Path(root)
    names = _members(
        root, only=only, exclude=frozenset({CONTEXT_FILE}) if for_extension else frozenset()
    )
    if spent.entries + len(names) > caps.entries:
        raise ContextTooLarge(
            f"This context holds {len(names)} files, {spent.entries} were already sent "
            f"in this session, and the server takes at most {caps.entries} archive entries.",
            hint="the caps count across the base context and every extension of one session",
        )
    entries: list[ContextFile] = []
    plain = spool.with_name(spool.name + ".tar")
    try:
        with (
            plain.open("wb") as handle,
            tarfile.open(fileobj=handle, mode="w|", format=tarfile.PAX_FORMAT) as tar,
        ):
            for name in names:
                source = root / name
                _check_member(name, source, caps)
                info = tarfile.TarInfo(name)
                info.size = source.stat().st_size
                info.mtime = 0
                info.mode = 0o644
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                with source.open("rb") as member:
                    tar.addfile(info, member)
                if name != CONTEXT_FILE:
                    # `context.yaml` is deliberately outside the integrity
                    # list: hashing it would readmit `created` and the
                    # constraint — which the ID rule excludes by name —
                    # through the back door.
                    entries.append(ContextFile(path=name, sha256=sha256_file(source)))
        raw = plain.stat().st_size
        if spent.decompressed_bytes + raw > caps.decompressed_bytes:
            raise ContextTooLarge(
                f"This context unpacks to {raw} bytes, {spent.decompressed_bytes} were "
                f"already sent in this session, and the server takes at most "
                f"{caps.decompressed_bytes} across a session.",
                hint="send a smaller context, or ask the operator for a larger budget",
            )
        compressor = zstandard.ZstdCompressor(
            level=ZSTD_LEVEL, write_checksum=False, write_content_size=True, threads=0
        )
        with (
            plain.open("rb") as source,
            spool.open("wb") as target,
            compressor.stream_writer(target, size=raw, closefd=False) as writer,
        ):
            while block := source.read(_BLOCK):
                writer.write(block)
    finally:
        plain.unlink(missing_ok=True)
    size = spool.stat().st_size
    if spent.compressed_bytes + size > caps.compressed_bytes:
        raise ContextTooLarge(
            f"This context compresses to {size} bytes, {spent.compressed_bytes} were "
            f"already sent in this session, and the server takes at most "
            f"{caps.compressed_bytes}.",
            hint="send a smaller context, or ask the operator for a larger budget",
        )
    return PackedContext(
        path=spool,
        size=size,
        sha256=sha256_file(spool),
        files=tuple(entries),
        members=names,
        decompressed=raw,
    )


#: The one line every PEM private key ends its header with, whatever the
#: key type in front of it — ``BEGIN PRIVATE KEY``, ``BEGIN EC PRIVATE
#: KEY``, ``BEGIN RSA PRIVATE KEY``, ``BEGIN OPENSSH PRIVATE KEY``.
_PRIVATE_KEY_MARKER = b"PRIVATE KEY-----"

#: File names this workbench has ever kept private key material under:
#: a plain key file (the ``--signing-key``/dashboard form) and the
#: project's ``secrets/firmware/mcuboot.yaml`` (ADR 0015 decision 8).
_PRIVATE_KEY_NAMES = ("signing.key", "mcuboot.yaml", "mcuboot.pem")


def _holds_private_key(source: Path) -> bool:
    """Whether *source* carries PEM private key material, streaming.

    Read in blocks with an overlap the length of the marker, so a header
    split across two reads is still found; never read whole, because a
    context member may be as large as the per-file cap.
    """
    overlap = b""
    with source.open("rb") as handle:
        while block := handle.read(_BLOCK):
            if _PRIVATE_KEY_MARKER in overlap + block:
                return True
            overlap = block[-len(_PRIVATE_KEY_MARKER) :]
    return False


def _check_member(name: str, source: Path, caps: IngressCaps) -> None:
    """Refuse a member the server would refuse, before it is sent."""
    if len(name.split("/")) > caps.path_depth:
        raise ContextTooLarge(
            f'"{name}" is deeper than the {caps.path_depth} path segments the server takes.',
            hint="context paths are shallow by design — model/, keys/ and patches/<layer>/",
        )
    if source.is_symlink() or not source.is_file():
        raise ContextTooLarge(
            f'"{name}" is not a regular file, and a context carries regular files only.',
            hint="a symlink or a device node in an archive is a way out of the directory "
            "it is unpacked into, so neither side accepts one",
        )
    measured = source.stat().st_size
    if measured > caps.file_bytes:
        raise ContextTooLarge(
            f'"{name}" is {measured} bytes and the server takes at most {caps.file_bytes} '
            "in one context file.",
            hint="split the file, or ask the operator for a larger per-file cap",
        )
    # The crown invariant, enforced where a key could actually travel.
    # The API has no slot a private key could be passed in, but a file
    # left in the context directory needs no slot: it is packed as
    # ordinary content, and the server's own whitelist takes *any* file
    # name under `keys/`. So the packer looks — by name, because these
    # are the names this workbench has ever kept private key material
    # under (the plain file of a --signing-key override, and the
    # project's secrets YAML of ADR 0015 §8) — and at the bytes, because
    # a stray key under another name is the same accident. The content
    # check catches the YAML form too: the PEM block inside it carries
    # the same marker.
    if name.rsplit("/", 1)[-1] in _PRIVATE_KEY_NAMES or _holds_private_key(source):
        raise PrivateKeyRefused(
            f'"{name}" holds private key material, and nothing was sent.',
            hint=(
                "the private signing key never leaves this machine: "
                "a build returns an unsigned image and the host signs it afterwards, so "
                "the only key a context carries is keys/signing.pub — move the private "
                "key out of the context directory"
            ),
        )


# --------------------------------------------------------------------------
# Safe extraction — the sibling of mcuhome.compiler.localbackend._safe_extract
# --------------------------------------------------------------------------


def _safe_member_name(name: str) -> str:
    """A tar member's path, or a refusal. Never normalized — refused.

    The same rule as :func:`mcuhome.compiler.localbackend._safe_member_name`,
    re-stated because a workbench must not import the compiler (ADR 0020
    decision 3). ``..`` and absolute paths are the escape; rewriting
    ``./x`` to ``x`` would accept a tree a stricter reader then refuses.
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
        raise RemoteTransportError(
            f"The artifact archive carries {name!r}, which is not a usable path.",
            hint=(
                "artifact paths are relative, use forward slashes and carry no empty, "
                ". or .. segment — anything else is a way out of the directory the "
                "archive is unpacked into"
            ),
        )
    return cleaned


def _contained(root: Path, relative: str) -> Path | None:
    """The absolute path of *relative* under *root*, or ``None``.

    Strict containment, checked segment by segment with ``lstat`` rather
    than with :meth:`Path.resolve` — the twin of
    :func:`mcuhome.compiler.localbackend._contained` and of the build
    server's own egress check. ``resolve`` answers where a path *leads*,
    which is the wrong question: a member that is a symlink to
    ``/etc/shadow`` resolves to a contained-looking absolute path only
    after the link has been followed. What matters is that no segment is
    a link at all.
    """
    segments = relative.split("/")
    if not segments or any(part in ("", ".", "..") for part in segments):
        return None
    current = root
    last = len(segments) - 1
    for index, part in enumerate(segments):
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            # Only the final segment may be missing — it is what is about
            # to be created. A missing *intermediate* segment is not a
            # containment answer, so the caller that needs one is
            # `_contained_for_write`, which makes the directory itself.
            return current if index == last else None
        if stat.S_ISLNK(info.st_mode):
            return None
        if index != last and not stat.S_ISDIR(info.st_mode):
            return None
    return current


def _contained_for_write(root: Path, relative: str) -> Path | None:
    """:func:`_contained`, creating the intermediate directories it walks.

    An artifact archive names members by their *declared* paths and
    carries no directory members for them — the build server packs one
    ``TarInfo`` per declared artifact and tar synthesizes no parents — so
    a legitimate ``zephyr/zephyr.hex`` arrives with nothing having
    created ``zephyr/`` first. Containment therefore has to make the
    directory as part of the walk rather than answer "not contained" for
    a path that is perfectly contained.

    The order is what keeps this safe: each segment is checked *before*
    the next one is created, with a plain ``mkdir`` — never
    ``parents=True, exist_ok=True``, which would follow a symlinked
    parent and create outside *root* the very tree this walk exists to
    prevent.
    """
    segments = relative.split("/")
    if not segments or any(part in ("", ".", "..") for part in segments):
        return None
    current = root
    last = len(segments) - 1
    for index, part in enumerate(segments):
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            if index == last:
                return current
            try:
                os.mkdir(current)
            except OSError:
                return None
            continue
        if stat.S_ISLNK(info.st_mode):
            return None
        if index != last and not stat.S_ISDIR(info.st_mode):
            return None
    return current


def _safe_extract(archive: Path, *, into: Path, quota_bytes: int) -> tuple[str, ...]:
    """Unpack a plain tar into *into*: regular files and directories only.

    Absolute paths, ``..``, symlinks, hardlinks and device nodes are
    rejected, and every target is re-checked for containment segment by
    segment before it is written — so a member cannot land outside *into*
    even by way of a directory some earlier member created. Modes are
    normalized to 0600: nothing in an artifact archive is executed, and
    honouring a mode would let an archive ask for a setuid bit.

    *quota_bytes* bounds the whole delivery and is counted **across**
    members while they are written, the way
    :func:`mcuhome.compiler.localbackend._safe_extract` counts it: a
    limit that fires only once the bytes are on the disk is not a limit.
    """
    into.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    total = 0
    try:
        with archive.open("rb") as raw, tarfile.open(fileobj=raw, mode="r|") as tar:
            for member in tar:
                name = _safe_member_name(member.name)
                target = _contained_for_write(into, name)
                if target is None:
                    raise RemoteTransportError(
                        f'The artifact archive entry "{name}" does not stay inside the '
                        "directory it is unpacked into.",
                        hint="a segment of the path is a symlink or is not a directory",
                    )
                if member.isdir():
                    target.mkdir(exist_ok=True)
                    continue
                if not member.isfile():
                    raise RemoteTransportError(
                        f'The artifact archive carries "{name}", which is not a regular file.',
                        hint=(
                            "a symlink, hardlink or device node in an archive is a way "
                            "out of the directory it is unpacked into"
                        ),
                    )
                source = tar.extractfile(member)
                if source is None:  # pragma: no cover - isfile() was true a line ago
                    raise RemoteTransportError(
                        f'The artifact archive entry "{name}" carries no data.', hint=""
                    )
                with target.open("wb") as handle:
                    while block := source.read(_BLOCK):
                        total += len(block)
                        if total > quota_bytes:
                            raise RemoteTransportError(
                                f"The artifact archive unpacks to more than {quota_bytes} bytes.",
                                hint=(
                                    "the delivery is corrupt or hostile — a build server "
                                    "is the least trusted component in this system and "
                                    "does not get to fill this disk"
                                ),
                            )
                        handle.write(block)
                target.chmod(0o600)
                written.append(name)
    except tarfile.TarError as error:
        raise RemoteTransportError(
            f"The artifact archive is not a readable tar ({error}).",
            hint="the transfer is corrupt — ask for the artifact again",
        ) from error
    except OSError as error:
        # A member the tar reads back fine but the filesystem refuses to
        # create — a component over NAME_MAX, a tree too deep, no space —
        # is not a `TarError` and would otherwise leave this function as
        # a bare `OSError`. The refusals raised above are not `OSError`
        # and pass through this arm untouched.
        raise RemoteTransportError(
            f"The artifact archive holds an entry this filesystem cannot unpack ({error}).",
            hint=(
                "an entry names a path the filesystem rejects, or the disk is full — the "
                "delivery is not usable as it arrived"
            ),
        ) from error
    return tuple(written)


def _decompress(archive: Path, plain: Path, *, limit: int) -> None:
    """zstd to a plain tar on disk, streaming, bounded by *limit*.

    Never one-shot: a few kilobytes of zstd expand to gigabytes, and a
    decompressor that returned its output as one ``bytes`` would have
    allocated the bomb before any check could run. The build server
    applies the identical shape to what a client sends it, and so does
    :func:`mcuhome.compiler.localbackend._decompress` — including the
    byte count, which is the half that makes the streaming worth
    anything: without it the bomb lands on the disk instead of in memory.
    """
    zstandard = _zstandard()
    written = 0
    with archive.open("rb") as raw, plain.open("wb") as handle:
        reader = zstandard.ZstdDecompressor().stream_reader(raw)
        while block := reader.read(_BLOCK):
            written += len(block)
            if written > limit:
                raise RemoteTransportError(
                    f"The artifact archive unpacks to more than {limit} bytes.",
                    hint=(
                        "the delivery is corrupt or hostile — a build server is the least "
                        "trusted component in this system and does not get to fill this disk"
                    ),
                )
            handle.write(block)


# --------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------

#: One line of an invocation's raw log, as it arrives. Contract §8 makes
#: standard output and standard error "one raw, opaque log stream" that a
#: consumer MUST NOT parse for machine decisions.
LineSink = Callable[[str], None]

#: One typed event of an invocation: its name and its whole payload,
#: relayed verbatim. An unknown name is delivered, never dropped — which
#: is what lets a third-party build container report its own phases under
#: ``x-`` names through a client that has never heard of them.
EventSink = Callable[[str, dict[str, Any]], None]


def _retrieved(future: asyncio.Future) -> None:
    """Mark a failed future's exception as seen. See :meth:`_fail_pending`."""
    with contextlib.suppress(Exception):
        future.exception()


@dataclass
class _Download:
    """One ``get-artifact`` in flight: the announcement, then the bytes.

    The correlation rule is order and nothing else — a BINARY frame
    carries no frame id — so the reader owns both halves: it fills the
    announcement in when the result frame goes past and appends every
    BINARY frame after it, which is the only arrangement in which a
    caller's turn on the event loop cannot fall between the two.
    """

    spool: Path
    frame_id: str
    done: asyncio.Future
    announcement: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()
    received: int = 0
    _handle: Any = None

    def announce(self, payload: dict[str, Any]) -> None:
        self.announcement = payload.get("archive") or {}
        declared = payload.get("artifacts")
        self.artifacts = artifacts_from_wire(declared) if isinstance(declared, list) else ()
        if self._expected > MAX_ARTIFACT_ARCHIVE_BYTES:
            # Refused from the announcement, before the first byte: a cap
            # that fires once the bytes are on the disk is not a cap.
            self.fail(
                RemoteTransportError(
                    f"The build server announced an artifact archive of {self._expected} "
                    f"bytes, and this client takes at most {MAX_ARTIFACT_ARCHIVE_BYTES}.",
                    hint=(
                        "nothing announces an egress cap, so this one is the client's own "
                        "— a delivery this large is a broken or hostile server"
                    ),
                )
            )
            return
        self._handle = self.spool.open("wb")
        if self._expected == 0:
            self.finish()

    @property
    def _expected(self) -> int:
        size = self.announcement.get("size")
        return size if isinstance(size, int) and not isinstance(size, bool) else 0

    def feed(self, data: bytes) -> None:
        if self._handle is None or self.done.done():
            return
        try:
            self._handle.write(data)
        except OSError as error:
            # The spool is local: a full disk is news about this machine
            # and must not be reported as — or mistaken for — a dead
            # socket, so only the download fails.
            self.fail(
                RemoteTransportError(
                    f"The artifact spool at {self.spool} could not be written ({error}).",
                    hint="this is local storage, not the connection — free some space and "
                    "ask for the artifact again",
                )
            )
            return
        self.received += len(data)
        if self.received >= self._expected:
            self.finish()

    def finish(self) -> None:
        self.close()
        if not self.done.done():
            self.done.set_result(self)

    def fail(self, error: BaseException) -> None:
        self.close()
        if not self.done.done():
            self.done.set_exception(error)

    def close(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.close()


class SessionClient:
    """One conversation with one build server: all eleven verbs.

    Async throughout (E16, E21): a build blocks for minutes to hours,
    every answer a caller waits on is awaitable, and cancellation is a
    verb rather than a closed socket. The state machine mirrors the
    server's — no context, unlocked, locked — because a client that
    guessed would discover a typed refusal where it could have known.

    **No parameter of this class or of any method on it takes a private
    key.** The public half travels as ``keys/signing.pub`` *inside the
    context* and is placed there by whoever created the context; the
    private half is a client-side secret that this class has no slot for.

    **Concurrent calls are safe, and one thing makes them so.** An
    announcement and the BINARY frames after it are correlated by order
    alone, so ``send-context``, ``extend-context`` and ``get-artifact``
    take :attr:`_sequence` for the whole of their sequence — one at a
    time per client, mirroring the build server's own per-connection
    lock. Everything else may interleave freely.

    Use it as an async context manager::

        async with SessionClient(url, token=token) as client:
            await client.capabilities()
            await client.open_session()
            ...
    """

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        on_event: EventSink | None = None,
        on_line: LineSink | None = None,
        spool_dir: Path | None = None,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self.url = url
        self._token = token
        self._on_event = on_event
        self._on_line = on_line
        self._spool_dir = Path(spool_dir) if spool_dir is not None else None
        self._call_timeout = call_timeout

        self._session: Any = None
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._download: _Download | None = None
        self._frames = 0
        #: Held across a whole announce-then-stream sequence, in both
        #: directions. A BINARY frame carries no id, so two of them at
        #: once are two byte streams nobody can separate afterwards —
        #: which the server documents on its own ``download_lock`` and
        #: enforces on its side; this is the client's half of it.
        self._sequence = asyncio.Lock()

        #: What ``capabilities`` answered, and the caps derived from it.
        self.capabilities_payload: Capabilities | None = None
        self.caps: IngressCaps = E44_CAPS
        #: What this session has already spent of those caps. The
        #: server's ledger is cumulative across the base context and
        #: every extension, so the client's has to be too.
        self.spent: IngressSpent = _NOTHING_SPENT

        #: The session, as this client knows it.
        self.session_id: str | None = None
        self.context_state: str = "none"
        self.context_id: str | None = None
        #: One-way: set by ``session.poisoned`` and by an E37 mismatch.
        #: Both are terminal for the session, and neither is retried into.
        self.terminal: str | None = None

        #: The integrity list of everything sent, path -> sha256. This is
        #: what the E37 comparison is computed over: "the ID of the bytes
        #: I sent" rather than "the ID of a directory as it is now".
        self._files: dict[str, str] = {}
        self._pins: Any = None
        #: Highest ``seq`` seen per invocation, which is what makes a
        #: replay after a reconnect lossless *and* duplicate-free.
        self._last_seq: dict[str, int] = {}
        self._finished: dict[str, asyncio.Future] = {}

    # ----------------------------------------------------------------
    # Connection
    # ----------------------------------------------------------------

    async def __aenter__(self) -> SessionClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the WebSocket. Idempotent for an already-open connection.

        A reconnect keeps the session state: the server's session
        outlives a socket by design — "connection loss is never
        abandonment" — so this is also the first half of resuming, and
        :meth:`attach_session` is the second.

        An open socket whose reader has ended is repaired rather than
        left: nothing but the reader takes frames off that socket, so a
        client in that state would send commands nobody ever answers.
        """
        if self._ws is not None and not self._ws.closed:
            self._start_reader()
            return
        aiohttp = _aiohttp()
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            self.url, headers=headers, max_msg_size=MAX_INBOUND_FRAME_BYTES
        )
        self._start_reader()

    def _start_reader(self) -> None:
        """Ensure exactly one live reader task for the current socket."""
        if self._reader is None or self._reader.done():
            self._reader = asyncio.create_task(self._read_loop(), name="mcuhome-session-reader")

    async def close(self) -> None:
        """Drop the transport. The *session* is closed by ``close-session``."""
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None
        self._fail_pending(
            RemoteTransportError("The connection to the build server is closed.", hint="")
        )

    # ----------------------------------------------------------------
    # The reader — one place where every inbound frame is sorted
    # ----------------------------------------------------------------

    async def _read_loop(self) -> None:
        aiohttp = _aiohttp()
        try:
            async for message in self._ws:
                if message.type is aiohttp.WSMsgType.BINARY:
                    if self._download is not None:
                        self._download.feed(message.data)
                    continue
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(message.data)
                except ValueError:
                    continue
                if isinstance(frame, dict):
                    self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # the socket died under us
            self._fail_pending(
                RemoteTransportError(
                    f"The connection to the build server failed: {error}.",
                    hint="reconnect and attach-session — a running invocation survives a "
                    "lost socket, and its events replay from the last seq you saw",
                )
            )
            return
        self._fail_pending(
            RemoteTransportError(
                "The build server closed the connection.",
                hint="reconnect and attach-session — the session outlives the socket",
            )
        )

    def _dispatch(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "event":
            self._dispatch_event(frame)
            return
        if kind == "log":
            payload = frame.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            if self._on_line is not None:
                self._deliver(lambda: self._on_line(str(payload.get("line", ""))), "on_line")
            return
        frame_id = frame.get("id")
        key = None if frame_id is None else str(frame_id)
        download = self._download
        if download is not None and key == download.frame_id and kind == "result":
            # The announcement is applied **here**, in the reader, so that
            # no caller's turn on the event loop can fall between it and
            # the BINARY frames it announces — which is the whole of the
            # correlation rule, because a BINARY frame carries no id.
            self._pending.pop(key, None)
            download.announce(frame.get("payload") or {})
            return
        pending = self._pending.pop(key, None) if key is not None else None
        if pending is not None and not pending.done():
            pending.set_result(frame)

    def _dispatch_event(self, frame: dict[str, Any]) -> None:
        name = str(frame.get("event") or "")
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        invocation_id = payload.get("invocation_id")
        seq = payload.get("seq")
        numbered = isinstance(seq, int) and not isinstance(seq, bool)
        if (
            isinstance(invocation_id, str)
            and numbered
            and seq <= self._last_seq.get(invocation_id, 0)
        ):
            # Already delivered — a replay boundary the server drew one
            # event too generously, or an overlap between replay and the
            # live join. Dropping it here is what makes "no event lost
            # and no event duplicated" true for the caller rather than
            # only for the wire.
            return
        if name == "invocation.verdict" and isinstance(invocation_id, str):
            # E46's *verdict*, and since E58 a name of its own. It used to
            # share `invocation.finished` with the program's contract §8
            # announcement and was told from it by the absence of `seq` —
            # so a program that omitted its counter had its own event read
            # as the server's judgement. The program's numbered
            # `invocation.finished` still arrives, and is delivered to the
            # caller's sink like any other event.
            self._note_verdict(payload)
            future = self._finished.setdefault(
                invocation_id, asyncio.get_running_loop().create_future()
            )
            if not future.done():
                future.set_result(payload)
        delivered = True
        if self._on_event is not None:
            delivered = self._deliver(lambda: self._on_event(name, payload), "on_event")
        if isinstance(invocation_id, str) and numbered and delivered:
            # Recorded *after* the sink took it, not before: an event the
            # caller never actually received must stay replayable, and
            # this number is what a resume asks the server for.
            self._last_seq[invocation_id] = seq

    def _note_verdict(self, payload: dict[str, Any]) -> None:
        """Read the terminal states a verdict carries (E39).

        The synchronous way a session poisons is a refused command, and
        :meth:`_answer` sets :attr:`terminal` for it. The *ordinary* way
        is asynchronous: an interrupted patch application ends the
        invocation, the server poisons the session while assembling the
        verdict, and the code travels in the verdict's error envelope.
        Reading it here is what keeps the attribute's promise true for
        both, so the next ``verify``/``build`` is refused locally instead
        of one wasted round trip later.
        """
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        if code == "session.poisoned":
            self.terminal = "session.poisoned"

    def _deliver(self, call: Callable[[], Any], what: str) -> bool:
        """Run a caller's sink, and never let it look like a dead socket.

        A sink is a consumer, not the transport. It runs on the reader
        task, so an exception from it would otherwise reach
        :meth:`_read_loop`'s ``except Exception`` — which fails every
        pending call with "the connection to the build server failed",
        ends the reader and leaves a healthy socket with nobody reading
        it. A ``BrokenPipeError`` from a sink writing to a closed stdout
        is the ordinary case, and it is news about the caller.
        """
        try:
            call()
        except Exception:
            _logger.exception("the %s sink of this session client raised", what)
            return False
        return True

    def _fail_pending(self, error: BaseException) -> None:
        """Hand *error* to everything waiting on this connection.

        The completion futures are failed too, because a client waiting
        for a verdict on a socket that is gone will wait forever
        otherwise — and each of them is armed with a retrieval callback,
        since a caller that finished with an invocation and never asked
        again is the normal case, not a swallowed bug.

        They are **dropped** as they are failed, and that is the half
        that makes the advertised recovery real: the session outlives the
        socket, so a caller that reconnects and re-attaches gets the
        verdict of an invocation that kept running. A poisoned future
        left in this dict would be handed back by
        :meth:`wait_finished`'s ``setdefault`` forever, and
        :meth:`_dispatch_event` would drop the real verdict on arrival
        because the future it belongs to is already done.
        """
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        if self._download is not None:
            self._download.fail(error)
            self._download = None
        for invocation_id, future in list(self._finished.items()):
            self._finished.pop(invocation_id, None)
            if not future.done():
                future.set_exception(error)
                future.add_done_callback(_retrieved)

    # ----------------------------------------------------------------
    # Sending
    # ----------------------------------------------------------------

    def _next_id(self) -> str:
        self._frames += 1
        return f"c-{self._frames}"

    def _require_open(self) -> None:
        if self._ws is None or self._ws.closed:
            raise RemoteTransportError(
                "This client is not connected to a build server.",
                hint="call connect() first, or use the client as an async context manager",
            )

    async def _call(
        self, verb: str, payload: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send one command frame and wait for the frame that answers it."""
        self._require_open()
        frame_id = self._next_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[frame_id] = future
        try:
            await self._ws.send_str(
                json.dumps(
                    {"id": frame_id, "type": verb, "payload": payload or {}},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            frame = await asyncio.wait_for(
                future, self._call_timeout if timeout is None else timeout
            )
        except TimeoutError:
            self._pending.pop(frame_id, None)
            raise RemoteTransportError(
                f'The build server did not answer "{verb}".',
                hint="the connection may be gone — reconnect and attach-session",
            ) from None
        finally:
            self._pending.pop(frame_id, None)
        return self._answer(verb, frame)

    def _answer(self, verb: str, frame: dict[str, Any]) -> dict[str, Any]:
        """One answering frame as a payload, or as the typed refusal it is."""
        if frame.get("type") == "error":
            error = frame.get("error") or {}
            code = str(error.get("code") or "")
            if code == "session.poisoned":
                self.terminal = code
                raise SessionPoisoned(error, verb=verb)
            raise ServerRefusal(error, verb=verb)
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            raise RemoteTransportError(
                f'The build server answered "{verb}" with a frame this client cannot read.',
                hint="the peer is not speaking session protocol 2",
            )
        return payload

    async def _send_archive(self, packed: PackedContext) -> None:
        """Stream a packed archive as BINARY frames, chunked to the caps.

        The smaller of the announced frame cap and this client's own
        conservative default wins. An announced cap *larger* than the
        default is a licence rather than an instruction — the default is
        already small enough that no reasonable peer refuses it — while a
        smaller one is a limit that must be obeyed, and obeying it is why
        the number is read from the announcement at all rather than
        written down here.
        """
        chunk = max(1, min(self.caps.frame_bytes, DEFAULT_CHUNK_BYTES))
        with packed.path.open("rb") as handle:
            while block := handle.read(chunk):
                await self._ws.send_bytes(block)

    # ----------------------------------------------------------------
    # The eleven verbs
    # ----------------------------------------------------------------

    async def capabilities(self) -> Capabilities:
        """``capabilities`` — the pre-session query. The handshake, first.

        It carries no session id because there is no session yet, and it
        is what tells this client which build containers the server has,
        which patch layers it allows and the ingress caps that size every
        upload afterwards (E57 — a peer that announces none leaves E44's
        defaults in place).
        """
        payload = await self._call("capabilities")
        self.capabilities_payload = Capabilities(payload)
        self.caps = self.capabilities_payload.ingress()
        return self.capabilities_payload

    async def open_session(
        self, *, profile: str = "oneshot", context_format: int = CONTEXT_VERSION
    ) -> dict[str, Any]:
        """``open-session`` — admission, and nothing about the context.

        Three operands and no fourth: the protocol version, the
        context-format version and the profile. The pins arrive later, in
        ``context.yaml``, because a session is admitted on no context at
        all.
        """
        payload = await self._call(
            "open-session",
            {
                "profile": profile,
                "protocol_version": SESSION_PROTOCOL_VERSION,
                "context_format": context_format,
            },
        )
        session = payload.get("session") or {}
        self.session_id = session.get("id")
        self.context_state = str(session.get("context_state") or "none")
        # A new session is a new ingress budget: the server's ledger is
        # per session, and so is this one.
        self.spent = _NOTHING_SPENT
        return payload

    def _require_session(self) -> str:
        if self.session_id is None:
            raise RemoteTransportError(
                "This client has no session on the build server yet.",
                hint="open-session first — it is what admission answers with an id",
            )
        if self.terminal is not None:
            raise RemoteError(
                f"This session is terminal ({self.terminal}) and can no longer do work.",
                hint=(
                    "collect what get-artifact still offers, close the session, and open a new one"
                ),
            )
        return self.session_id

    async def send_context(self, context_dir: Path) -> dict[str, Any]:
        """``send-context`` — the base context and its pins, once (E41, E43).

        The archive is announced in the JSON payload — its compressed size
        and its SHA-256 — and the bytes follow as BINARY frames; the
        result frame *is* the acknowledgement. A second ``send-context``
        is refused (``context.exists``): one base context per session, and
        a fresh start is a new session, which is cheap.

        The context's ``context.yaml`` is read here as well, because its
        pins are two of the three inputs of the context ID and the E37
        comparison needs them locally.
        """
        session_id = self._require_session()
        context_dir = Path(context_dir)
        self._pins = read_context_request(context_dir / CONTEXT_FILE)
        spool = self._spool(f"context-{uuid.uuid4().hex}.tar.zst")
        try:
            # Off the event loop: packing is tar plus a SHA-256 per member
            # plus zstd-19 over as much as the caps allow, and none of it
            # awaits. On the loop it would hold up this client's own
            # heartbeat — the pong is emitted from the reader — and every
            # other task of a process that embeds this client.
            packed = await asyncio.to_thread(
                pack_context, context_dir, spool=spool, caps=self.caps, spent=self.spent
            )
            payload = await self._upload("send-context", session_id, packed)
        finally:
            spool.unlink(missing_ok=True)
        self.spent = self.spent.plus(packed)
        self._files = {entry.path: entry.sha256 for entry in packed.files}
        self.context_state = str((payload.get("context") or {}).get("state") or "unlocked")
        return payload

    async def extend_context(
        self,
        context_dir: Path | None = None,
        *,
        paths: Sequence[str] | None = None,
        remove: Iterable[str] = (),
    ) -> dict[str, Any]:
        """``extend-context`` — an archive and a remove list, in one call (E42).

        Both halves are optional and at least one is required; an
        extension that changes nothing is a command that meant something
        else. Order within one call is removals first, then the archive,
        so a path named in both ends up as the archive's version — the
        archive is the positive statement of what the context should
        contain.

        ``context.yaml`` is untouchable in both directions
        (``context.pins-immutable``): it carries the pins the session was
        admitted on, and changing them is a new session. Naming it in
        *paths* is refused here, before a frame goes out; leaving *paths*
        out entirely extends the context by everything under
        *context_dir*, and the pin file is excluded from that archive —
        which is what makes the whole-directory extension the legal call
        the verb's shape says it is, instead of a full upload the server
        then refuses.
        """
        session_id = self._require_session()
        removals = tuple(dict.fromkeys(remove))
        if context_dir is None and not removals:
            raise RemoteError(
                "An extension needs files to add, paths to remove, or both.",
                hint="an extend-context that changes nothing is a command that meant "
                "something else",
            )
        for path in removals:
            if path == CONTEXT_FILE:
                raise RemoteError(
                    "An extension may not remove context.yaml.",
                    hint=(
                        "it carries the pins this session was admitted on, and changing "
                        "them is a new session rather than an extension"
                    ),
                )
        if paths is not None and CONTEXT_FILE in {_named_member(path) for path in paths}:
            raise RemoteError(
                "An extension may not carry context.yaml.",
                hint=(
                    "it carries the pins this session was admitted on, and changing "
                    "them is a new session rather than an extension"
                ),
            )
        packed: PackedContext | None = None
        spool = self._spool(f"extend-{uuid.uuid4().hex}.tar.zst")
        try:
            extra: dict[str, Any] = {}
            if removals:
                extra["remove"] = list(removals)
            if context_dir is not None:
                packed = await asyncio.to_thread(
                    pack_context,
                    Path(context_dir),
                    spool=spool,
                    caps=self.caps,
                    only=paths,
                    for_extension=True,
                    spent=self.spent,
                )
                payload = await self._upload("extend-context", session_id, packed, **extra)
            else:
                payload = await self._call("extend-context", {"session_id": session_id, **extra})
        finally:
            spool.unlink(missing_ok=True)
        for path in removals:
            self._files.pop(path, None)
        if packed is not None:
            self.spent = self.spent.plus(packed)
            self._files.update({entry.path: entry.sha256 for entry in packed.files})
        return payload

    async def _upload(
        self, verb: str, session_id: str, packed: PackedContext, **extra: Any
    ) -> dict[str, Any]:
        """Announce, stream, and take the acknowledgement.

        The announcement and the bytes are one sequence on the wire: a
        BINARY frame carries no id, so the only thing that says which
        upload its bytes belong to is *when* they arrive. The command
        frame therefore goes out and the chunks follow it immediately,
        without awaiting the answer in between — and the whole sequence
        is held under :attr:`_sequence`, because a second upload's chunks
        interleaved into this one are bytes the server hashes into a
        ``context.integrity-mismatch`` nobody can explain afterwards.
        """
        async with self._sequence:
            self._require_open()
            frame_id = self._next_id()
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[frame_id] = future
            try:
                await self._ws.send_str(
                    json.dumps(
                        {
                            "id": frame_id,
                            "type": verb,
                            "payload": {
                                "session_id": session_id,
                                "archive": packed.announcement(),
                                **extra,
                            },
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                await self._send_archive(packed)
                frame = await asyncio.wait_for(future, self._call_timeout)
            except TimeoutError:
                raise RemoteTransportError(
                    f'The build server never acknowledged "{verb}".',
                    hint="the upload may have been cut off — open a new session and retry",
                ) from None
            finally:
                self._pending.pop(frame_id, None)
        return self._answer(verb, frame)

    async def lock_context(self) -> str:
        """``lock-context`` — freeze the context, and **check the ID** (E37).

        The verb's own wire shape is minimal by explicit product-owner
        choice: the request carries the session id, the answer carries the
        context ID, and the server never sees this client's value. The
        comparison ADR 0019 requires therefore happens **here**, and this
        method is the reason the freeze is an "observable moment" at all.

        The local value is computed over the integrity list of the bytes
        this client actually sent — the base context plus every accepted
        extension, minus every removal — and over the pins out of
        ``context.yaml``, which is exactly the three-part document the
        frozen rule hashes.

        A disagreement **closes the session** and raises
        :class:`ContextIdMismatch` naming both values. There is nothing to
        salvage: an artifact built from that context would be attributed
        to an identity that does not describe it.
        """
        session_id = self._require_session()
        payload = await self._call("lock-context", {"session_id": session_id})
        remote = str(payload.get("context_id") or "")
        local = self.compute_context_id()
        if remote != local:
            with contextlib.suppress(Exception):
                await self.close_session()
            self.terminal = "context.id-mismatch"
            raise ContextIdMismatch(local=local, remote=remote)
        self.context_id = remote
        self.context_state = "locked"
        return remote

    def compute_context_id(self) -> str:
        """The context ID of the bytes this client sent, by the frozen rule.

        The rule lives in ``mcuhome-model`` and is called over *values*
        rather than over a directory (ADR 0020 decision 4), which is what
        lets both sides of the contract compute it from what they each
        hold: the server from the bytes it received off a socket, this
        client from the integrity list it built while packing.
        """
        if self._pins is None:
            raise RemoteError(
                "This client has sent no context, so there is no identity to compute.",
                hint="send-context carries context.yaml, and its pins are two of the "
                "three inputs of the context ID",
            )
        files = tuple(
            ContextFile(path=path, sha256=digest) for path, digest in sorted(self._files.items())
        )
        return context_id(
            sdk_sha256=self._pins.sdk.sha256,
            board=self._pins.board,
            files=files,
        )

    async def verify(self) -> str:
        """``verify`` — check the effective context, from the lock onwards.

        A working command, so before the lock it is ``context.not-locked``
        — which is what makes it mean anything: the freeze writes the
        ``files`` integrity list, so there is something stable to check
        against. Answers an invocation id immediately; the verdict arrives
        as an event.
        """
        return await self._start("verify", {})

    async def build(self, *, mode: str = "clean") -> str:
        """``build [mode]`` — clean or incremental. Answers an invocation id.

        ``clean`` is the default and the safe one: it never silently
        reuses state, and it is what a release artifact requires. The
        build itself returns an **unsigned** image plus the §7.2.1 build
        report (E55, E56); signing is a host-side step afterwards and this
        client never performs it and never carries a key for it.
        """
        return await self._start("build", {"mode": mode})

    async def _start(self, verb: str, extra: dict[str, Any]) -> str:
        session_id = self._require_session()
        payload = await self._call(verb, {"session_id": session_id, **extra})
        invocation_id = str(payload.get("invocation_id") or "")
        if not invocation_id:  # pragma: no cover - the server always answers one
            raise RemoteTransportError(
                f'The build server answered "{verb}" without an invocation id.', hint=""
            )
        self._finished.setdefault(invocation_id, asyncio.get_running_loop().create_future())
        return invocation_id

    async def cancel(self, invocation_id: str) -> dict[str, Any]:
        """``cancel(invocation id)`` — acknowledged immediately (E38).

        The answer means "the stop signal is set", never "the invocation
        has stopped": the actual end travels on the event stream and the
        result document carries ``status: cancelled``. An invocation that
        already finished answers ``already_finished`` and is **not** an
        error — a cancel racing a natural completion is legitimate and
        both parties behaved correctly.

        The session and its warm container survive; a closed socket is
        never a stop signal, which is why the verb exists.
        """
        # Deliberately not gated on :attr:`terminal`: cancel stops work
        # rather than doing any, and a poisoned session may still have an
        # invocation worth stopping — which is exactly the server's own
        # rule for this verb.
        if self.session_id is None:
            raise RemoteTransportError("This client has no session.", hint="open-session first")
        return await self._call(
            "cancel", {"session_id": self.session_id, "invocation_id": invocation_id}
        )

    async def wait_finished(
        self, invocation_id: str, *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Wait for the server's ``invocation.verdict`` (E46, E58).

        Not a verb — a build is minutes to hours, and a command frame that
        waited for it would make every client's socket a build timer. The
        verb acknowledges, the completion arrives on the channel that
        survives a reconnect, and this is where a caller waits for it.

        **The verdict is the server's frame and has its own name.** The
        program's contract §8 ``invocation.finished`` — numbered like
        every program event, emitted immediately before the result
        document is written — arrives first and is delivered as an
        ordinary event; it is not what this method returns. Only the
        verdict carries the status, the artifact list, the context ID the
        server computed and, on a failure, the error envelope. E46 first
        gave both frames one name and left ``seq`` to separate them, which
        made a program's §8 violation readable as the server's judgement;
        E58 replaced that with the name.
        """
        future = self._finished.setdefault(
            invocation_id, asyncio.get_running_loop().create_future()
        )
        if timeout is None:
            return await future
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            raise RemoteTransportError(
                f'The invocation "{invocation_id}" did not finish within {timeout:.0f} seconds.',
                hint="cancel it, or attach-session again and keep following its events",
            ) from None

    async def get_artifact(
        self,
        invocation_id: str,
        *,
        into: Path,
        path: str | None = None,
        expected: dict[str, str] | None = None,
    ) -> ArtifactDelivery:
        """``get-artifact`` — a tar.zst, announced then streamed (E45).

        The mirror of the upload: the result frame *is* the announcement
        (the archive's size, its SHA-256 and what is in it) and the BINARY
        frames follow it. With *path* the archive holds exactly that
        declared artifact; without one it holds every declared artifact of
        the invocation under its declared path.

        Three checks, and each answers a different question. The archive's
        hash is the transport check, against the announcement. The
        per-file hashes are the integrity check, against *expected* — the
        artifact list this client already holds from the verdict, which is
        what keeps the announced hash from becoming a second integrity
        claim that could disagree with the first. And extraction is
        contained segment by segment, because ``out`` is written by the
        least trusted component in the system — as is the *size* of what
        it sends, which is why the announcement is refused above
        :data:`MAX_ARTIFACT_ARCHIVE_BYTES` and the expansion above
        :data:`MAX_ARTIFACT_BYTES`.

        Held under :attr:`_sequence` from the command frame to the last
        byte: the announcement and the bytes are correlated by order,
        exactly as in the upload direction.

        Runs **inside the session**: the per-session directory and every
        artifact in it are destroyed at ``close-session``.
        """
        session_id = self.session_id
        if session_id is None:
            raise RemoteTransportError("This client has no session.", hint="open-session first")
        into = Path(into)
        async with self._sequence:
            self._require_open()
            spool = self._spool(f"artifact-{uuid.uuid4().hex}.tar.zst")
            plain = spool.with_name(spool.name + ".tar")
            frame_id = self._next_id()
            future: asyncio.Future = asyncio.get_running_loop().create_future()
            download = _Download(spool=spool, frame_id=frame_id, done=future)
            self._pending[frame_id] = future
            self._download = download
            payload: dict[str, Any] = {"session_id": session_id, "invocation_id": invocation_id}
            if path is not None:
                payload["path"] = path
            try:
                await self._ws.send_str(
                    json.dumps(
                        {"id": frame_id, "type": "get-artifact", "payload": payload},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
                answered = await asyncio.wait_for(future, self._call_timeout)
                if isinstance(answered, dict):
                    # An error frame carries the same id as the announcement
                    # would have, so a refusal resolves the very future the
                    # bytes would otherwise have completed.
                    self._answer("get-artifact", answered)
                declared = download.announcement.get("sha256")
                # Off the event loop, all three of them: hashing,
                # decompressing and unpacking a firmware set is the same
                # kind of blocking work as packing a context, and the
                # heartbeat this client owes the server is emitted from
                # the reader task.
                measured = await asyncio.to_thread(sha256_file, spool)
                if declared != measured:
                    raise RemoteTransportError(
                        "The artifact archive does not hash to the value the server "
                        f"announced: {measured} against {declared}.",
                        hint="the transfer is corrupt — ask for the artifact again",
                    )
                await asyncio.to_thread(_decompress, spool, plain, limit=MAX_ARTIFACT_BYTES)
                written = await asyncio.to_thread(
                    _safe_extract, plain, into=into, quota_bytes=MAX_ARTIFACT_BYTES
                )
            finally:
                self._pending.pop(frame_id, None)
                self._download = None
                download.close()
                spool.unlink(missing_ok=True)
                plain.unlink(missing_ok=True)
        members = tuple(download.artifacts)
        await asyncio.to_thread(_check_members, into, members, expected)
        return ArtifactDelivery(
            invocation_id=invocation_id,
            root=into,
            files=written,
            artifacts=members,
            sha256=str(download.announcement.get("sha256") or ""),
            size=int(download.announcement.get("size") or 0),
        )

    async def attach_session(
        self, *, invocation_id: str | None = None, from_seq: int | None = None
    ) -> dict[str, Any]:
        """``attach-session`` — connection loss is not abandonment.

        Re-joins the session's live stream and, when an invocation is
        named, replays its events from the sequence number this client
        last saw — out of the NDJSON file on disk, which **is** the replay
        buffer (E46); there is no in-memory ring behind it and therefore
        nothing a long reconnect can find already evicted.

        The replayed events arrive **before** this verb's own answer, so
        the boundary is a boundary: every event frame before the answer is
        history and everything after it is live. *from_seq* defaults to
        one past the highest ``seq`` this client has seen for that
        invocation, which is the number that makes the resume lossless;
        the duplicate half is handled here as well, because a boundary
        drawn one event too generously must not reach a caller twice.
        """
        session_id = self.session_id
        if session_id is None:
            raise RemoteTransportError("This client has no session.", hint="open-session first")
        payload: dict[str, Any] = {"session_id": session_id}
        if invocation_id is not None:
            payload["invocation_id"] = invocation_id
            wanted = from_seq if from_seq is not None else self._last_seq.get(invocation_id, 0) + 1
            payload["from_seq"] = wanted
        answer = await self._call("attach-session", payload)
        session = answer.get("session") or {}
        self.context_state = str(session.get("context_state") or self.context_state)
        return answer

    async def close_session(self) -> dict[str, Any]:
        """``close-session`` — release the session and reap its container.

        The stop signal is set for every running invocation, the container
        is removed and the per-session directory is deleted with the
        context and every artifact in it — which is why ``get-artifact``
        has to run before this. Permitted in any context state; the lock
        does not gate the way out.
        """
        session_id = self.session_id
        if session_id is None:
            raise RemoteTransportError("This client has no session to close.", hint="")
        try:
            return await self._call("close-session", {"session_id": session_id})
        finally:
            self.session_id = None
            self.context_state = "none"

    # ----------------------------------------------------------------

    def _spool(self, name: str) -> Path:
        root = self._spool_dir
        if root is None:
            root = Path(tempfile.mkdtemp(prefix="mcuhome-remote-"))
            self._spool_dir = root
        root.mkdir(parents=True, exist_ok=True)
        return root / name


def _check_members(
    root: Path, artifacts: tuple[Artifact, ...], expected: dict[str, str] | None
) -> None:
    """Re-hash every delivered artifact against what the invocation declared.

    E45's per-file half: the announced hash covers the archive, and the
    hashes that say whether a *firmware image* is the one that was built
    are the ones the client already holds from the verdict. *expected*
    overrides the announcement's own list where a caller has one, which
    is the stronger check — the announcement travelled with the bytes it
    describes, the verdict did not.
    """
    for entry in artifacts:
        path = entry.path
        declared = (expected or {}).get(path) or entry.sha256
        target = _contained(root, path)
        if target is None or not target.is_file():
            raise RemoteTransportError(
                f'The artifact archive did not deliver "{path}", which it declared.',
                hint="ask for the artifact again, or for the whole set at once",
            )
        if isinstance(declared, str) and sha256_file(target) != declared:
            raise RemoteTransportError(
                f'The delivered artifact "{path}" does not hash to the value the build '
                "declared for it.",
                hint=(
                    "declared hashes are advisory and the bytes decide — this delivery is "
                    "not the artifact the invocation reported"
                ),
            )


@dataclass(frozen=True)
class ArtifactDelivery:
    """One ``get-artifact``, unpacked and checked.

    :attr:`root` is where the files actually are, and every artifact's
    ``path`` is relative to it — the same relationship
    :attr:`mcuhome.compiler.localbackend.LocalOutcome.out` has to its own
    artifact list, so a caller that signs an image afterwards does not
    have to know which build method produced it — including the *type* of
    that list, which is :class:`mcuhome.model.artifacts.Artifact` on both
    sides.
    """

    invocation_id: str
    root: Path
    files: tuple[str, ...]
    artifacts: tuple[Artifact, ...]
    sha256: str
    size: int


# --------------------------------------------------------------------------
# The composition — the shape slice 4 dispatches on
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteBuildResult:
    """What one remote invocation produced, in ``LocalOutcome``'s shape.

    The fields a dispatcher over the three build methods reads are the
    same fields with the same meanings and the same *types*:
    :attr:`action`, :attr:`context_id`, :attr:`status`,
    :attr:`successful`, :attr:`artifacts` — a tuple of
    :class:`mcuhome.model.artifacts.Artifact`, the very class
    :attr:`~mcuhome.compiler.localbackend.LocalOutcome.artifacts` carries
    — and :attr:`out`, where those files are on this machine.
    :attr:`context_id` is the identity the work is attributed to, the one
    the *server* computed and this client already compared against its
    own (E37).

    Two fields are this method's own and have no local counterpart:
    :attr:`error`, the refusal envelope out of the verdict, and
    :attr:`invocation_id`. Four of ``LocalOutcome``'s have no remote
    counterpart — ``exit_code``, ``result``, ``problems`` and
    ``violation`` are what a backend sees of a container it started
    itself, and a client that invented them from a verdict would be
    reporting things it did not observe.
    """

    action: str
    context_id: str
    status: str
    successful: bool
    artifacts: tuple[Artifact, ...]
    out: Path | None
    error: dict[str, Any] | None = None
    invocation_id: str = ""


async def run_remote_build(
    context_dir: Path,
    *,
    url: str,
    work_root: Path,
    token: str | None = None,
    action: str = "build",
    mode: str = "clean",
    profile: str = "oneshot",
    on_line: LineSink | None = None,
    on_event: EventSink | None = None,
    timeout: float | None = None,
) -> RemoteBuildResult:
    """Drive one invocation through a build server and take the artifacts back.

    The composition, in order: connect, ``capabilities`` (which sizes the
    upload), ``open-session``, ``send-context``, ``lock-context`` with the
    E37 comparison, the working verb, follow the events until the
    verdict, ``get-artifact`` into ``work_root/out``, ``close-session``.

    *action* is ``build`` or ``verify``, the same two
    :meth:`mcuhome.compiler.localbackend.LocalBackend.run` takes and the
    same two the server implements; *mode* applies to ``build`` only.

    It mirrors that method: a context directory and a work root in, an
    **unsigned** artifact set plus the build report out, progress through
    a line sink. Signing is the shared host-side step afterwards (E55,
    E56) and no key is a parameter of this function, of the client it
    drives, or of any frame either of them sends.
    """
    if action not in ("build", "verify"):
        raise RemoteError(
            f'"{action}" is not something a build server can be asked to do.',
            hint="the working verbs of the session protocol are build and verify",
        )
    # The delivery directory is emptied first, exactly as `LocalBackend.run`
    # empties the invocation directory it is about to fill: an old
    # `out/firmware.bin` that this build does not re-declare would survive
    # here, be undeclared and therefore unchecked, and be what a host
    # signer scanning this directory afterwards finds.
    out = Path(work_root) / "out"
    await asyncio.to_thread(shutil.rmtree, out, ignore_errors=True)
    async with SessionClient(
        url, token=token, on_line=on_line, on_event=on_event, spool_dir=Path(work_root) / "spool"
    ) as client:
        await client.capabilities()
        await client.open_session(profile=profile)
        try:
            await client.send_context(Path(context_dir))
            identity = await client.lock_context()
            if action == "verify":
                invocation_id = await client.verify()
            else:
                invocation_id = await client.build(mode=mode)
            verdict = await client.wait_finished(invocation_id, timeout=timeout)
            status = str(verdict.get("status") or "failure")
            artifacts = artifacts_from_wire(verdict.get("artifacts") or ())
            delivered: Path | None = None
            if artifacts:
                expected = {entry.path: entry.sha256 for entry in artifacts}
                await client.get_artifact(invocation_id, into=out, expected=expected)
                delivered = out
            return RemoteBuildResult(
                action=action,
                context_id=str(verdict.get("context") or identity),
                status=status,
                successful=status == "success",
                artifacts=artifacts,
                out=delivered,
                error=verdict.get("error"),
                invocation_id=invocation_id,
            )
        finally:
            with contextlib.suppress(Exception):
                await client.close_session()
