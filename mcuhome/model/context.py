# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build context — the self-contained input artifact of a remote build.

A build context is a plain directory: ``manifest.yaml``, the canonical
device model under ``model/``, optionally patches under
``patches/<layer>/``. It contains everything a build needs except the
build environment (a build container carrying a toolchain and Zephyr)
and the SDK (fetched as a hash-pinned package). It travels as an
archive, but the archive is transport: the directory is the artifact.

**The client states a requirement; the backend resolves it** (E61). A
context does not name a container — it names the **Zephyr line** its
model was resolved against, and choosing a container that carries that
line is the backend's job. The backend writes what it chose — image,
tag, digest — into ``manifest.yaml``, outside the identity, and *that*
is what reproduces the build years later. The alternative, a client
that pins a container digest, asks the wrong party: a client knows
which Zephyr it needs and cannot know which images a given build server
serves, so a digest chosen by the client is either a guess or a
round-trip that has to happen before a context can exist at all.

**The context ID is normative — fixed with ``context`` format version 2,
and it can never change afterwards.** Everything that ever names a
context — integrity verification, artifact attribution ("built from
*this*"), archival references — depends on computing the same ID from
the same inputs forever. The rule is stated in
``docs/design/build-container-contract.md`` §3.3 (ADR 0018); this
module implements it. The ID is the SHA-256 over the canonical JSON
(RFC 8785) of exactly this document::

    {"files": [{"path": ..., "sha256": ...}, ...],
     "sdk": {"sha256": ...},
     "target": {"board": ...}}

— the manifest's build-relevant fields under the contract's fixed
names (``sdk.sha256`` carries the manifest's
``mcuhome.package.sha256``). ``files`` is sorted by ``path`` in
ascending byte order of its UTF-8 encoding — which UTF-8 makes equal
to code-point order, so a plain string sort implements it — and every
listed file contributes its own content hash; the sort only makes the
encoding deterministic.

Explicitly excluded, so they can never influence the ID: ``created``
(informational), ``mcuhome.constraint`` (the intent, not the
resolution), ``mcuhome.version`` and ``mcuhome.package.url`` (names for
the bytes ``package.sha256`` already pins), the whole ``container``
block (the backend's resolution, not the client's statement — see
below), and ``zephyr``.

``zephyr`` is excluded because hashing it would be **redundancy, not
identity**. The line is provably inside two things the ID already
hashes: it is a property of the SDK release that ``sdk.sha256`` pins
(SDK and Zephyr move as one — the SDK repository *is* the west
manifest, ADR 0006/0008), and it is a field of the canonical device
model, whose bytes are an ordinary entry of ``files``. Two contexts
that differ only in their required line therefore already have two
IDs, and adding a third copy of the same fact to the hashed document
would buy nothing while making the document larger to agree about. The
field is carried in the two documents anyway, informationally, so a
build server can pick a container without parsing the model file.

``manifest.yaml`` itself and the backend-written ``.mcuhome/`` runtime
directory are never integrity entries, so they cannot influence the ID
either. Neither the YAML file bytes nor the transport archive bytes are
ever hashed — neither serialization is deterministic. New
build-relevant fields enter the hash only together with a bump of the
``context`` format version.

Patches carry no manifest section of their own. A patch is a file: its
target layer is its subfolder, its application order is its ``NNNN-``
filename prefix, and its integrity entry sits in ``files`` like every
other file's. There is deliberately no declared patch list that could
disagree with the files actually present, so a build server re-derives
its patch policy from the paths alone.

**This module is the format and the rule; it never touches a
filesystem.** Creating a context directory, hashing what is in one and
:func:`~mcuhome.workbench.contextdir.verify_context` — the server-side integrity
primitive that recomputes every file hash and the ID from the bytes
actually present — are :mod:`mcuhome.workbench.contextdir`. The cut is ADR 0020's:
the build server recomputes a context ID from bytes it received off a
socket and must carry no build logic to do it, so :func:`context_id`
takes values and not a directory, and this module imports nothing but
the standard library and :mod:`mcuhome.model.errors`.

:data:`CONTEXT_ID_VECTORS` is the conformance suite that keeps a second
implementation honest — the frozen rule stated as inputs and outputs
rather than as code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from mcuhome.model.errors import BuildError

__all__ = [
    "BACKEND_DIR",
    "CONTEXT_FILE",
    "KEYS_DIR",
    "SIGNING_KEY_FILE",
    "CONTEXT_ID_VECTORS",
    "CONTEXT_VERSION",
    "MANIFEST_FILE",
    "MODEL_FILE",
    "PATCHES_DIR",
    "ContainerResolution",
    "ContextFile",
    "ContextManifest",
    "ContextRequest",
    "SdkPin",
    "canonical_json",
    "context_id",
    "validate_manifest",
    "validate_zephyr_line",
    "vector_id",
]

#: Format version of the context manifest. The normative hashing rule is
#: locked to it: a field can join the hashed document only together with
#: a bump here, and version 2's rule never changes.
#:
#: Version 1 pinned a container digest and hashed it. It is gone rather
#: than supported alongside this one (E61): nothing is published, no
#: context written to version 1 exists outside a test, and a reader that
#: accepted both formats would have to carry two hashing rules forever
#: to serve exactly zero documents.
CONTEXT_VERSION = 2

#: The one file a builder must parse first, at the top of the context.
MANIFEST_FILE = "manifest.yaml"

#: The request document with the pins, next to the manifest (§3.2). It
#: is excluded from the integrity list **as a statement about the hash,
#: not about layout**: its never-hashed fields (constraint, url, zephyr,
#: created) would otherwise leak into an identity that §6 computes from
#: resolved values alone.
CONTEXT_FILE = "context.yaml"

#: Where the MCUboot verification key lives inside a context (ADR 0018's
#: 2026-08-09 amendment; required for ``build``, §7.2).
KEYS_DIR = "keys"
SIGNING_KEY_FILE = "keys/signing.pub"

#: Where the canonical device model lives inside the context — the
#: existing wire format (:mod:`mcuhome.model.model`), unchanged.
MODEL_FILE = "model/device-model.json"

#: Where patches live: ``patches/<layer>/NNNN-name.patch``.
PATCHES_DIR = "patches"

#: The backend-written runtime directory inside a mounted context
#: (``.mcuhome/command.json``), as contract v1 was first drafted.
#: Plumbing, not content: never an integrity entry, never identity.
#:
#: **The contract has moved on and this constant has not yet.** The
#: per-invocation request document now lives in a backend-owned
#: directory outside the context, and there is no ``.mcuhome/`` in a
#: context at all (build-container-contract.md §3.1, §5.2) — which is
#: what makes the context a genuinely read-only mount. Removing the
#: directory from the exclusion rules is migration work; keeping it
#: excluded in the meantime is harmless, because a context that never
#: contains one cannot be affected by the exclusion.
BACKEND_DIR = ".mcuhome"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")

#: A Zephyr release line, as ``zephyr:`` carries it: dotted numeric
#: components and nothing else — ``4.4``, ``4.4.0``, ``5``. A *line* is
#: a prefix of a release, so the grammar is the release's grammar minus
#: the upstream suffix an actual release may carry (``4.5.0-rc1``): a
#: requirement is written against the numbered line, and a pre-release
#: suffix is a property of one container, never of a line.
_ZEPHYR_LINE = re.compile(r"[0-9]+(\.[0-9]+)*\Z")


# --------------------------------------------------------------------------
# The manifest, as data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextFile:
    """One integrity entry: a context-relative path and its content hash."""

    #: Relative to the context directory, forward slashes on every
    #: platform. Part of the hashed identity, so its spelling is checked
    #: strictly (:func:`context_id` refuses anything else).
    path: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class SdkPin:
    """The resolved mcuhome pin — the manifest's ``mcuhome:`` section.

    ``constraint`` is the original intent from the device configuration
    (a PEP 440 specifier such as ``~=2.3.6`` — ADR 0018's PEP 440
    amendment); ``version`` and the package are what it resolved to at
    context creation. Only ``sha256`` is part of the context's identity:
    the constraint is intent, and the version and URL are names for the
    bytes the hash already pins.
    """

    constraint: str
    version: str
    #: Where the resolved SDK package can be fetched. A hint only — a
    #: server resolves (version, sha256) against its own source list.
    url: str
    sha256: str


@dataclass(frozen=True)
class ContainerResolution:
    """Which build container a backend chose — the manifest's ``container:``.

    **Written by the backend at ``lock-context``, never by the client,
    and never hashed** (E61). It is the answer to the context's
    ``zephyr`` requirement: the backend picked a container of that line
    out of the ones it serves, and this records which. That is what makes
    a build reproducible years later — the requirement says what was
    needed, this says what actually ran.

    ``digest`` is the repo digest, and it is ``None`` for an image that
    was built locally and never pushed. Such an image names no pinnable
    bytes, which is a fact about it rather than a defect: recording
    ``null`` says exactly that, whereas the placeholder digest the
    digest-pinned format needed (because a hashed field could not be
    absent) said something untrue about bytes nobody can fetch.
    """

    image: str
    tag: str
    digest: str | None

    @staticmethod
    def from_reference(reference: str, *, digest: str | None) -> ContainerResolution:
        """Record the image a backend resolved, given how docker names it.

        The split is best-effort and may be, because none of the three
        fields is identity: a trailing ``:tag`` carrying no ``/`` is the
        tag, and a reference with none — or one whose colon is a registry
        port — is recorded against ``latest``, which is what docker itself
        means by an unqualified name.

        A reference that already names a digest (``repo@sha256:…``, which
        is a perfectly ordinary thing to hand a backend) has that part
        taken off first. Without that step the ``@`` form splits on the
        digest's own colon and records an image called ``repo@sha256``
        under a tag of 64 hex digits — a record that names nothing.

        It lives here rather than in either backend because both do it,
        and a manifest whose ``image``/``tag`` split depends on which
        backend wrote it would be two spellings of one record.
        """
        name, at_sign, _ = reference.partition("@")
        if at_sign:
            # A digest reference carries no tag to recover, and inventing
            # one would be worse than `latest`: `latest` says "nothing was
            # said", a made-up tag would say something false.
            return ContainerResolution(image=name, tag="latest", digest=digest)
        name, separator, tail = reference.rpartition(":")
        if separator and "/" not in tail:
            return ContainerResolution(image=name, tag=tail, digest=digest)
        return ContainerResolution(image=reference, tag="latest", digest=digest)

    def reference(self) -> str:
        """How to name this image again: by digest where there is one."""
        return f"{self.image}@{self.digest}" if self.digest else f"{self.image}:{self.tag}"


@dataclass(frozen=True)
class ContextManifest:
    """``manifest.yaml``, as an object."""

    sdk: SdkPin
    #: The required Zephyr release line, repeated from the request —
    #: informational, never hashed. It stands next to :attr:`container`
    #: on purpose: requirement and resolution side by side, so a human
    #: reading a manifest back sees both what was asked for and what
    #: answered.
    zephyr: str
    #: What the backend resolved that requirement to.
    container: ContainerResolution
    #: The target board — the manifest's ``target:`` section.
    board: str
    #: The integrity list: every file in the context except the manifest
    #: itself, patches included, sorted by path.
    files: tuple[ContextFile, ...]
    #: ``sha256:<hex>`` — the declared context ID. Advisory like every
    #: declared value; :func:`verify_context` recomputes it.
    id: str
    context_version: int = CONTEXT_VERSION

    def compute_id(self) -> str:
        """The ID this manifest's hashed fields yield, per the normative rule."""
        return context_id(
            sdk_sha256=self.sdk.sha256,
            board=self.board,
            files=self.files,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context_version,
            "mcuhome": {
                "constraint": self.sdk.constraint,
                "version": self.sdk.version,
                "package": {"url": self.sdk.url, "sha256": self.sdk.sha256},
            },
            "zephyr": self.zephyr,
            "container": {
                "image": self.container.image,
                "tag": self.container.tag,
                "digest": self.container.digest,
            },
            "target": {"board": self.board},
            "files": [entry.to_dict() for entry in self.files],
            "id": self.id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ContextManifest:
        mcuhome = data["mcuhome"]
        package = mcuhome["package"]
        container = data["container"]
        digest = container["digest"]
        # ``created`` is deliberately not read: it dates the *request* and
        # lives in context.yaml alone — "the one field that does not
        # travel" (ADR 0018). A manifest that carries one anyway is
        # handled by the unknown-field rule: ignored.
        return ContextManifest(
            context_version=int(data["context"]),
            sdk=SdkPin(
                constraint=str(mcuhome["constraint"]),
                version=str(mcuhome["version"]),
                url=str(package["url"]),
                # Hashes and paths are deliberately not coerced: they are
                # identity, and _validate_manifest type-checks them.
                sha256=package["sha256"],
            ),
            zephyr=data["zephyr"],
            container=ContainerResolution(
                image=str(container["image"]),
                tag=str(container["tag"]),
                # Not coerced either, and for the opposite reason: the
                # absence of a digest is meaningful, so `None` has to
                # survive the round trip as `None` and not as "None".
                digest=digest,
            ),
            board=data["target"]["board"],
            files=tuple(
                ContextFile(path=item["path"], sha256=item["sha256"]) for item in data["files"]
            ),
            id=data["id"],
        )


@dataclass(frozen=True)
class ContextRequest:
    """``context.yaml``, as an object — the pinning *request* (ADR 0018 amendment).

    The client-written half of the split ADR 0018 decision 6's single
    document became: the ``lock-context`` freeze splits the manifest into
    a *request* and a *result* (build-container-contract.md §3.2), and
    this is the request. It carries the ``context`` format version, the
    resolved SDK pin, the Zephyr line the build environment has to carry
    and the original intent the session is admitted on — and deliberately
    **nothing that depends on the final file set**: no ``files`` list and
    no ``id``. Those are the result the locking party computes and writes
    into :class:`ContextManifest`.

    It has **no ``container`` block at all** (E61). A request states what
    it needs; which container answers is the backend's to decide and the
    backend's to record, in :class:`ContainerResolution` on the manifest.
    That is what makes the request writable by a client that has never
    seen the machine it will build on.

    It repeats :class:`SdkPin` verbatim — the same pin
    :class:`ContextManifest` later restates, so intent and resolution
    stand side by side where a human reads back what was asked for
    (decision 3) — and adds ``created``. That timestamp is "the one field
    that does not travel" (ADR 0018): it dates the request, is never
    hashed, and lives here alone.
    """

    sdk: SdkPin
    #: The Zephyr release line the build environment must carry — the
    #: canonical model's ``toolchain.zephyr_line`` (ADR 0013), spelled
    #: out here so a build server can select a container without parsing
    #: the model file. **Informational: never hashed** (see the module
    #: docstring on why that is redundancy rather than a hole).
    zephyr: str
    #: The target board — the request's ``target:`` section.
    board: str
    #: The instant the request was created, as an ISO 8601 UTC string
    #: (e.g. ``2026-08-10T09:00:00Z``). Informational and never hashed;
    #: carried as text so two requests differ only where their ``created``
    #: does, and so the value round-trips through YAML without a parser
    #: reinterpreting it as a native timestamp.
    created: str
    context_version: int = CONTEXT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context_version,
            "created": self.created,
            "mcuhome": {
                "constraint": self.sdk.constraint,
                "version": self.sdk.version,
                "package": {"url": self.sdk.url, "sha256": self.sdk.sha256},
            },
            "zephyr": self.zephyr,
            "target": {"board": self.board},
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ContextRequest:
        mcuhome = data["mcuhome"]
        package = mcuhome["package"]
        return ContextRequest(
            context_version=int(data["context"]),
            sdk=SdkPin(
                constraint=str(mcuhome["constraint"]),
                version=str(mcuhome["version"]),
                url=str(package["url"]),
                # Not coerced: the sha256 is identity, spelling-checked
                # where it is used, not silently normalized on read.
                sha256=package["sha256"],
            ),
            # Not coerced, for the reason the hashes are not: an
            # unquoted `zephyr: 4.4` is a YAML float, and str() would
            # turn a document that says a number into one that says a
            # line. validate_zephyr_line is what says so.
            zephyr=data["zephyr"],
            board=data["target"]["board"],
            created=str(data["created"]),
        )


# --------------------------------------------------------------------------
# The normative hash
# --------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """*value* as RFC 8785 canonical JSON — for this module's value domain.

    The hashed document contains only objects with fixed ASCII key
    names, arrays, and string values. For that domain Python's own
    serializer *is* the JSON Canonicalization Scheme: keys sorted
    (code-point order, which equals JCS's UTF-16 order for ASCII keys),
    minimal separators, strings escaped the ECMAScript way — the
    two-character escapes, lowercase ``\\u00xx`` for the remaining
    control characters, everything else emitted literally — and the
    UTF-8 encoding done by the caller. Deliberately this small instead
    of a full JCS implementation: JCS's only hard part is number
    serialization, and numbers never occur in the document.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require_sha256(value: object, *, what: str) -> None:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise BuildError(
            f"{what} is not a SHA-256 hash: {value!r}.",
            hint=(
                "the canonical spelling is exactly 64 lowercase hex digits — one "
                "spelling per hash, so two manifests can never name the same bytes "
                "differently"
            ),
        )


def _require_digest(value: object, *, what: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise BuildError(
            f"{what} is not a sha256 digest: {value!r}.",
            hint='the canonical form is "sha256:" followed by 64 lowercase hex digits',
        )


def validate_zephyr_line(value: object) -> None:
    """Check the ``zephyr:`` field's spelling, or refuse naming the value.

    Public because two parties parse the field and only one of them can
    build a whole manifest around it: a build server holding a freshly
    read ``context.yaml`` needs the same check the manifest validator
    makes, and re-spelling the grammar on that side would be a second
    implementation of it. The rule is not part of the hash — a
    build-container contract §3.3.1 question this field never asks —
    but it *is* part of what a backend matches a container against, and
    a line nobody can match is better refused where it arrives.
    """
    if not isinstance(value, str) or _ZEPHYR_LINE.fullmatch(value) is None:
        raise BuildError(
            f"{value!r} is not a Zephyr release line.",
            hint=(
                "a line is dotted decimal numbers and nothing else — 4.4 for the "
                "4.4.x releases. It carries no leading v (west's spelling) and no "
                "pre-release suffix: a suffix belongs to one release, never to a "
                "line. Write it quoted (zephyr: '4.4'), because YAML reads an "
                "unquoted 4.4 as a number and a number is not a line"
            ),
        )


def _require_board(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BuildError(
            "The context names no target board.",
            hint=(
                "the board is a hashed identity field — a context that does not say "
                "what it builds for has no identity"
            ),
        )


def _require_path(value: object) -> None:
    usable = (
        isinstance(value, str)
        and value
        and "\\" not in value
        and not value.startswith("/")
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )
    if not usable:
        raise BuildError(
            f"{value!r} is not a usable context path.",
            hint=(
                "context paths are relative, use forward slashes and contain no "
                '"." or ".." segments — they are hashed identity and extraction '
                "targets at once"
            ),
        )


def _require_files(entries: Iterable[ContextFile]) -> None:
    seen: set[str] = set()
    for entry in entries:
        _require_path(entry.path)
        _require_sha256(entry.sha256, what=f'The hash of "{entry.path}"')
        if (
            entry.path in (MANIFEST_FILE, CONTEXT_FILE)
            or entry.path.split("/", 1)[0] == BACKEND_DIR
        ):
            raise BuildError(
                f'The integrity list must not name "{entry.path}".',
                hint=(
                    "manifest.yaml describes the list and .mcuhome/ is backend "
                    "plumbing — the contract keeps both out of the context's "
                    "identity (build-container-contract.md §3.2)"
                ),
            )
        if entry.path in seen:
            raise BuildError(
                f'The integrity list names "{entry.path}" twice.',
                hint="one entry per file — a duplicate would make the canonical encoding ambiguous",
            )
        seen.add(entry.path)


def context_id(
    *,
    sdk_sha256: str,
    board: str,
    files: Iterable[ContextFile],
) -> str:
    """The context ID — SHA-256 over the canonical form of the hashed fields.

    This function is the normative rule of the module docstring, locked
    with ``context`` format version 2. It takes exactly the three hashed
    inputs and nothing else, so an informational field *cannot* leak
    into the ID by construction — which is the same discipline version 1
    had, applied to a shorter list: the container is no longer an input
    because the client no longer chooses it (E61). Inputs are checked
    strictly rather than normalized: an ID computed over a mistyped hash
    would be silently wrong forever, and normalizing (say, uppercase hex)
    would give the same bytes two names.
    """
    entries = tuple(files)
    _require_sha256(sdk_sha256, what="The SDK package hash")
    _require_board(board)
    _require_files(entries)
    document = {
        "files": [entry.to_dict() for entry in sorted(entries, key=lambda entry: entry.path)],
        "sdk": {"sha256": sdk_sha256},
        "target": {"board": board},
    }
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_manifest(manifest: ContextManifest) -> None:
    """Check a parsed manifest's shape and spelling, not its truth.

    Every hashed field, spelled the one way the format allows, plus the
    declared ID and the two fields that are carried rather than hashed:
    the ``zephyr`` requirement and the ``container`` resolution that
    answered it. The unhashed pair is checked for the same reason the
    hashed ones are — a manifest is read by build containers this
    project does not write, and a value nobody can parse is worth
    refusing where it is read rather than where it is used — but the
    check is deliberately weaker where the format allows less: a
    resolution may carry ``digest: null``, because an image that was
    never pushed names no bytes.

    Whether the declared values match bytes on a disk is
    :func:`~mcuhome.workbench.contextdir.verify_context`'s question and needs a
    disk to answer.
    """
    _require_sha256(manifest.sdk.sha256, what="The SDK package hash")
    validate_zephyr_line(manifest.zephyr)
    _require_resolution(manifest.container)
    _require_board(manifest.board)
    _require_files(manifest.files)
    _require_digest(manifest.id, what="The context id")


def _require_resolution(resolution: ContainerResolution) -> None:
    """The backend's ``container:`` block, spelled the one legal way."""
    for value, what in ((resolution.image, "image"), (resolution.tag, "tag")):
        if not isinstance(value, str) or not value.strip():
            raise BuildError(
                f"The container resolution names no {what}.",
                hint=(
                    "manifest.yaml records which build container answered the "
                    "context's zephyr requirement — image, tag and digest — and a "
                    "record that names no image records nothing"
                ),
            )
    if resolution.digest is not None:
        _require_digest(resolution.digest, what="The container digest")


# --------------------------------------------------------------------------
# Conformance vectors
# --------------------------------------------------------------------------

#: The frozen rule, stated as inputs and outputs.
#:
#: ADR 0019 §8 obliges the build server to recompute the context ID from
#: the bytes it received, and ADR 0018 §6 says both sides of the contract
#: compute the same value. Whoever writes the second implementation — a
#: third-party build container, a server in another language, a future
#: rewrite of this one — needs something to be wrong against that is not
#: this file's source code. These are it: run :func:`context_id` (or your
#: own) over each ``inputs`` and you must get ``id``.
#:
#: **Version 2's vectors never change.** A vector may be added; an
#: existing one may not be altered, because altering one would mean the
#: rule changed, and the rule changing means a new ``context`` format
#: version (:data:`CONTEXT_VERSION`) with vectors of its own. They
#: cover what an independent implementation gets wrong: an empty
#: file list (the document still has the key), the ordinary single-file
#: case, a list whose given order is not its hashed order, a path
#: with non-ASCII characters — which :func:`canonical_json` emits
#: literally rather than as ``\\u`` escapes, and which therefore only
#: hashes the same if the other side agrees about that — and the two
#: concerns *together*, which is the third thing and the one no single
#: vector could state: the sort is over code points (equivalently, over
#: UTF-8 bytes) and **not** over UTF-16 code units. The two orders agree
#: for the whole BMP and disagree the moment a path outside it meets one
#: inside it, and UTF-16 order is the plausible mistake rather than an
#: exotic one — RFC 8785, which this module's hashed document *is*, uses
#: exactly that order for object keys, so an implementation that reaches
#: for its JCS library's comparator for the ``files`` array gets it
#: wrong. The "astral and BMP paths" vector is the one that catches it;
#: every other vector in this table a UTF-16 sort passes.
#:
#: The first five were regenerated for format version 2, which is the
#: only thing that may cause a vector's ID to change and is exactly what
#: a version bump is for: the hashed document lost its ``container``
#: member (E61), so every ID over the same files and pins is a different
#: number. Their **inputs** were kept otherwise identical to version 1's
#: on purpose — a diff of this table then shows one member gone and five
#: hashes moved, rather than a new table nobody can compare against the
#: old one. The sixth is new, and adding one is the only way that gap
#: could be closed: an existing vector may not be altered.
#:
#: The board names below are hash inputs and nothing else: the rule never
#: looks a board up, so a vector stays valid after the registry drops the
#: board it happens to name. Renaming one here would change an ID.
CONTEXT_ID_VECTORS: tuple[dict[str, Any], ...] = (
    {
        "name": "no files",
        "inputs": {
            "sdk_sha256": "a" * 64,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (),
        },
        "id": "sha256:e4a688665db1f01eccd6b0586193428adff1fa1c7077089fcf5fe621de061028",
    },
    {
        "name": "one file",
        "inputs": {
            "sdk_sha256": "b" * 64,
            "board": "nrf52840dk/nrf52840",
            "files": (("model/device-model.json", "c" * 64),),
        },
        "id": "sha256:63ae37786bc923be4ea4cbd5911282b3cf8ad6c5139cfdb89154512054ea1b3b",
    },
    {
        "name": "given out of order",
        "inputs": {
            "sdk_sha256": "d" * 64,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (
                ("patches/zephyr/0002-b.patch", "2" * 64),
                ("model/device-model.json", "1" * 64),
                ("patches/zephyr/0001-a.patch", "3" * 64),
                ("keys/signing.pub", "4" * 64),
            ),
        },
        "id": "sha256:75435b832983f6ead9c57aa600cb64bfdb7ffacc8171dac13534537e206ad9c6",
    },
    {
        # The vector tests_py/test_context.py has pinned since the format
        # was written, kept where a second implementation can reach it.
        "name": "model and one patch",
        "inputs": {
            "sdk_sha256": "cd" * 32,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (
                ("model/device-model.json", "11" * 32),
                ("patches/zephyr/0001-fix.patch", "22" * 32),
            ),
        },
        "id": "sha256:11affc97a03519f03745793f96c02d40d8b0216b0c57772d94cec5f322f8abcf",
    },
    {
        "name": "non-ascii path",
        "inputs": {
            "sdk_sha256": "e" * 64,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (("model/dévice-modèl.json", "5" * 64),),
        },
        "id": "sha256:82030ef8f50da6b415db90fc22996aeab67557896a35d806abd35130defc4989",
    },
    {
        # The two paths straddle the BMP: U+FF21 FULLWIDTH LATIN CAPITAL
        # LETTER A is inside it, U+1F600 GRINNING FACE is not, and UTF-16
        # spells the second one as a surrogate pair from U+D800 — below
        # U+FF21. So code-point order puts the fullwidth A first and
        # UTF-16 code-unit order puts the emoji first, and an
        # implementation that sorted the array the way RFC 8785 sorts
        # object keys computes a different ID here and the same ID
        # everywhere else in this table. Given emoji-first, so that a
        # sort that never runs is caught too.
        #
        # Written as escapes rather than as the characters themselves —
        # the whole vector turns on *which* character each one is, and a
        # fullwidth A that looked like an ASCII A would be a trap in the
        # trap. The paths are "model/" + U+FF21 + ".json" and "model/" +
        # U+1F600 + ".json"; the bytes hashed are their UTF-8.
        "name": "astral and BMP paths",
        "inputs": {
            "sdk_sha256": "f" * 64,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (
                ("model/\U0001f600.json", "7" * 64),
                ("model/\uff21.json", "6" * 64),
            ),
        },
        "id": "sha256:9b87cd6b9766d50fead4bd7b37b9f9fc503f92f259bddcb1b653e97a16abb972",
    },
)


def vector_id(vector: dict[str, Any]) -> str:
    """Run one :data:`CONTEXT_ID_VECTORS` entry through :func:`context_id`.

    A vector states its files as ``(path, sha256)`` pairs rather than as
    :class:`ContextFile` objects, so that the data stays copyable into a
    document another implementation can read.
    """
    inputs = vector["inputs"]
    return context_id(
        sdk_sha256=inputs["sdk_sha256"],
        board=inputs["board"],
        files=[ContextFile(path=path, sha256=sha256) for path, sha256 in inputs["files"]],
    )
