# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The invocation ABI of the build container contract, ``describe``, ``verify``.

``docs/design/build-container-contract.md`` §5 is frozen. A backend runs

    /mcuhome/run <action> <absolute path of the request document>

and reads a JSON result document back from the path the request named.
This module is that ABI: argv, the request document's five parsing rules
(§5.2), the three exit codes (§5.3), and the atomic result document
(§5.4). ``containers/builder/run`` is the thin launcher the image
installs at that fixed path; everything it does is call :func:`main`.

**Two actions are implemented: ``describe`` (§7.1) and ``verify``
(§7.3).** ``build`` is not, and it refuses the way §7 says an
unimplemented action refuses — ``status: "unsupported"``, ``reason:
"unsupported.action"``, exit 1 — which is a legible answer a backend can
reschedule on rather than a crash.

``verify`` computes nothing itself. Every hash and the effective context
ID come from :func:`~mcuhome.contextdir.verify_context`, the one
implementation of §3.3's normative rule in this project: "Implementations
on both sides of the contract MUST compute the ID independently from the
bytes they actually hold", which is only worth anything while each side
has *one* implementation. A second one here is the failure ADR 0018 §6
freezes the rule against.

Where the contract is silent this module does the smallest thing, and
says which line it is standing on. The full list:

*A document that is not RFC 8259 as this contract narrows it is exit 66.*
§5.2 opens with "UTF-8 without BOM, one JSON object, RFC 8259. Duplicate
keys are invalid. ``null`` never means 'absent'; it is invalid." — three
sentences about the same thing, the document's own definition. Parsing
rule 5 says exit 66 when the document "does not parse", so a duplicate
key and a ``null`` are read as "does not parse" rather than as
``unsupported.request``. The alternative would mean reading ``result``
out of a document just declared invalid, which is unanswerable in the one
case that matters: a document with two ``result`` keys.

*A ``result`` that is absent, not a string, or not absolute is exit 66.*
Rule 4 ("A path value that is not absolute ⇒ ``unsupported.request``")
cannot be honoured for ``result`` itself: writing that answer needs a
result path to write it to, and §5.1 forbids relying on a ``cwd`` to make
a relative one absolute. Rule 5's "``result`` is missing or not writable"
is the case that remains, and this is it.

*"Not writable" is found out by writing.* §5.4 makes the result document
"the **last write action** of the invocation", so this module does not
probe the directory first; it builds the whole document in memory and
reports exit 66 when the atomic write fails. Nothing is left behind
either way.

*The order of the checks is the bootstrap chain of §5.1*, step for step:
argv arity (3), parse (4), preamble (5), action (6), ``required`` (7),
the remaining fields (8).

*``program.actions`` lists what is implemented, not what is required.*
§7.1.1 calls it "the action names the program implements" and in the same
breath requires it to "include ``describe``, ``verify`` and ``build``".
Both cannot hold for a program that implements two of the three. Listing
an action that answers ``unsupported.action`` would be the worse lie of
the two — a backend MUST NOT invoke what is absent from the list, so the
honest list costs a refusal nobody has to reschedule. This is also why
the image carries no ``org.mcuhome.contract`` label yet: it is not a
conforming image, and it does not claim to be.

*What ``verify`` needs out of the request document.* §5.2 makes seven
fields mandatory for every working action, and rule 3 refuses over a
narrower thing: "A field the program needs for this action and does not
find". ``verify`` needs two of the seven. ``context`` is the directory
§7.3 defines the action over. ``session`` is needed because §5.4 makes it
mandatory in a ``verify`` result *and* forbids inventing one — "a program
MUST NOT invent a value for a field it was never given" — so a ``verify``
without it has no conforming result to write, and refusing is the only
answer left. The other five (``out``, ``work``, ``tmp``, ``trees.sdk``,
``limits.jobs``) name work this action does not do: it "reads the context
and nothing else" (§7.3), and §4.1 says "The program MUST NOT require an
entry it does not need for the requested action". A backend that omits
them is in breach of §5.2, but that is a defect in the backend's request
and not in this invocation's answer, and this program is not the thing
that reports it.

*A manifest that cannot be read at all is ``error.context.mismatch``.*
The registry of §5.4 defines eleven reasons and none of them is "the
manifest is corrupt": ``error.context.incomplete`` is "missing a file the
action needs", which a present-but-unreadable manifest is not, and
``unsupported.context`` is reserved for a format version this program
does not implement. What is left is the one ``verify`` failure §7.3
names — "measured the materialized context and it is not the context
``manifest.yaml`` describes" — which holds of a manifest that describes
no context at all. **This is a gap in contract v1**, recorded here rather
than papered over: a backend cannot tell a corrupt manifest from a
tampered file by ``reason`` alone, only from ``error.message``.

*A manifest that states no format version is not ``unsupported.context``
either.* That reason means the program "found a ``context`` format
version it does not implement", and §3.2 explains the status by "nothing
about this context is broken". A manifest with no ``context`` key is
broken, and answering ``unsupported`` would tell a backend to go and find
another image for a context no image can read. It is the previous case.

*The key names inside ``error.details``.* The contract fixes exactly one
— "``error.details.required`` for ``unsupported.required``" (§5.4.1) —
and otherwise says only which *facts* go there. So: ``context`` carries
"the version it found" (§3.2), under the name the manifest key already
has; ``missing`` carries "the missing path" (§7.2); ``paths`` carries
"the offending paths" (§7.3). ``paths`` is empty when the disagreement
names no content file — a spoofed ``id``, or a manifest that could not be
read — because ``manifest.yaml`` is not a content file (§3.2) and naming
it under "the offending paths" would put a file in that list that can
never be in the integrity list.

*A failing ``verify`` reports ``context`` exactly when it measured one.*
§5.4's table qualifies the row with "MUST, on success" and the paragraph
below it says what the qualification is for: "The rows qualified 'on
success' are the ones that report *measured* work. An invocation that
failed before it got that far reports what it measured and nothing more:
… a ``verify`` that could not read ``manifest.yaml`` has no effective
context ID." The criterion is measurement, not status — so an integrity
mismatch, which measured the effective ID on the way to finding it,
reports it, and a manifest this program refused to hash does not.
``layers`` never appears at all: §5.4 forbids it for ``verify``, because
"it reports work that was actually done, and ``verify`` does not do that
work".

*``trees`` is read from the image's own record.* The image writes
``/mcuhome/workspace.json`` (``containers/builder/workspace-record.py``,
whose docstring already anticipates "a later ``describe`` fills its
``trees`` block by lookup rather than by translation"). Without that file
— the ``subprocess`` profile, a foreign filesystem — every layer is
reported with ``"path": null``, which §7.1.1 defines as "this tree is not
in my image; put it wherever you like and name it in ``trees``". That is
true of a program that carries no build environment of its own, and it is
the answer that makes a backend supply what it would have had to supply
anyway.

This module reads no process state: ``argv`` arrives as an argument, and
every path it touches comes out of the request document. That is what
keeps it off the exemption list of ``tests_py/test_userpaths.py``.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcuhome import __version__
from mcuhome.context import MANIFEST_FILE
from mcuhome.contextdir import ContextFormatVersionError, verify_context
from mcuhome.errors import BuildError

__all__ = [
    "CONTRACT_VERSION",
    "EXIT_FAILURE",
    "EXIT_SUCCESS",
    "EXIT_UNUSABLE",
    "HONOURED_REQUIRED",
    "IMPLEMENTED_ACTIONS",
    "LAYERS",
    "PROGRAM_ID",
    "REQUEST_VERSIONS",
    "RESULT_VERSION",
    "RESULT_VERSIONS",
    "WORKSPACE_RECORD",
    "main",
    "program",
    "trees",
]

# --------------------------------------------------------------------------
# What this program is (§7.1.1)
# --------------------------------------------------------------------------

#: The contract version implemented here. This is contract v1.
CONTRACT_VERSION = 1

#: Request format versions this program can parse (§7.1.1 ``request``).
REQUEST_VERSIONS = (1,)

#: Result format versions this program can write (§7.1.1 ``result``).
RESULT_VERSIONS = (1,)

#: The one written, out of :data:`RESULT_VERSIONS`.
RESULT_VERSION = RESULT_VERSIONS[0]

#: A stable identifier of the *implementation*, reverse-DNS, opaque to a
#: backend (§7.1.1). Not of an image, a tag, a version or a vendor.
PROGRAM_ID = "org.mcuhome.build-container"

#: Every action this program implements, and the whole of ``describe``'s
#: ``program.actions``. See the module docstring for why ``build`` is not
#: in it.
IMPLEMENTED_ACTIONS = ("describe", "verify")

#: The layer registry of contract v1 §1.1, in the order ``describe``
#: reports them. Third-party layers carry an ``x-`` prefix and reach the
#: block only by way of the image's own record.
LAYERS = ("zephyr", "sdk", "chip", "mcuboot")

#: What the image says about the west workspace it carries
#: (``containers/builder/workspace-record.py``). Absent everywhere else,
#: which the module docstring covers. ``/mcuhome/`` is the namespace §2.2
#: reserves for this project inside an image, so this is not a promise
#: about somebody else's filesystem.
WORKSPACE_RECORD = Path("/mcuhome/workspace.json")

# --------------------------------------------------------------------------
# The frozen exit codes (§5.3)
# --------------------------------------------------------------------------

#: The invocation ran and the work succeeded; result document present.
EXIT_SUCCESS = 0

#: The invocation ran and the work did not succeed; result document
#: present, with a ``status`` that is not ``success``.
EXIT_FAILURE = 1

#: The request was unusable; no result could be addressed, nothing written.
EXIT_UNUSABLE = 66

_STATUS_SUCCESS = "success"
_STATUS_FAILURE = "failure"
_STATUS_UNSUPPORTED = "unsupported"

_REASON_REQUEST = "unsupported.request"
_REASON_REQUIRED = "unsupported.required"
_REASON_ACTION = "unsupported.action"
_REASON_CONTEXT = "unsupported.context"
_REASON_INCOMPLETE = "error.context.incomplete"
_REASON_MISMATCH = "error.context.mismatch"

# --------------------------------------------------------------------------
# The request document (§5.2)
# --------------------------------------------------------------------------


class _Unusable(Exception):
    """No result can be addressed: exit 66, nothing written (§5.3).

    Not an error type from :mod:`mcuhome.errors`, on purpose. Those render
    themselves for a person reading a terminal; this one is never rendered
    anywhere, because the whole point of exit 66 is that there is no
    channel to say anything on.
    """


#: Top-level fields whose value is a path (§5.2). ``result`` is not among
#: them: it is checked in the preamble, where a relative one is exit 66
#: rather than a refusal nobody could read (see the module docstring).
_PATH_FIELDS = ("out", "work", "tmp", "context", "events", "cancel")

#: Fields carrying one ``{path, …}`` object, and fields carrying a map of
#: them. ``trees`` is the map (§4.1); ``ccache`` is the single object (§10).
_PATH_OBJECTS = ("ccache",)
_PATH_OBJECT_MAPS = ("trees",)

#: "Absent" as distinct from "present and null" — although a request
#: document can never carry the latter, since a ``null`` anywhere in it is
#: exit 66 before any pointer is resolved.
_MISSING = object()


def _is_request_version(value: Any) -> bool:
    """A request format version this program parses.

    ``bool`` is excluded explicitly: it is an ``int`` in Python, and
    ``"request": true`` would otherwise be read as version 1.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value in REQUEST_VERSIONS


def _is_absolute_path(value: Any) -> bool:
    """A path value as §5.2 defines one: a string, and absolute."""
    return isinstance(value, str) and Path(value).is_absolute()


#: The JSON Pointers this program honours, each with the values it can
#: honour there (§5.2 rule 2: "knowing the path is not enough").
#:
#: Four, one per thing this program actually acts on. A pointer in this
#: list is a promise to act on the *value* found there, so each is here
#: for a reason of its own:
#:
#: * ``/request`` — acted on: a version outside :data:`REQUEST_VERSIONS`
#:   is refused rather than parsed hopefully.
#: * ``/result`` — acted on: the result document is written to exactly
#:   that path, which is why the value has to be an absolute one.
#: * ``/session`` — echoed, which is the whole of what §5.2 permits
#:   anyone to do with it, and honourable only as the opaque *string*
#:   token the contract defines. A backend demanding that a number be
#:   honoured there is told which pointer failed.
#: * ``/context`` — read: it is the directory ``verify`` is defined over
#:   (§7.3), and an absolute path is the only value that names one, since
#:   §5.1 forbids resolving a relative one against a ``cwd``.
#:
#: Everything else stays out, and ``verify``'s own mandatory fields are
#: the interesting half of that. ``/out``, ``/work``, ``/tmp``,
#: ``/trees/sdk`` and ``/limits/jobs`` reach this program on every
#: ``verify`` (§5.2 makes them mandatory for a working action) and it does
#: nothing whatever with them: §7.3's ``verify`` "reads the context and
#: nothing else", §9.2 point 10 forbids it to write into ``work`` or a
#: tree, and it declares no artifacts, so there is no value at any of
#: those pointers it could promise to honour. ``/params/mode`` belongs to
#: ``build``, which this program does not implement. A backend that
#: demands any of them is told so instead of being quietly served
#: something else.
HONOURED_REQUIRED: dict[str, Callable[[Any], bool]] = {
    "/request": _is_request_version,
    "/result": _is_absolute_path,
    "/session": lambda value: isinstance(value, str),
    "/context": _is_absolute_path,
}


def _is_present(value: Any) -> bool:
    """Anything at all, as opposed to :data:`_MISSING`."""
    return value is not _MISSING


#: What each action needs to find in the request document, as a JSON
#: Pointer and the values that are usable there. Rule 3 of §5.2: "A field
#: the program needs for this action and does not find … ⇒ ``status:
#: "unsupported"``, ``reason: "unsupported.request"``".
#:
#: ``describe`` "needs only the preamble" (§5.2) and so is absent here.
#: ``verify`` needs two of the seven fields §5.2 makes mandatory for a
#: working action, and the module docstring says why the other five are
#: not demanded back. ``/session`` is checked for presence and not for
#: type: §5.4's echo rule says a program echoes what it was given,
#: verbatim, and §5.2 forbids composing a path from it, so its type never
#: has to be believed. A backend that wants the token's type honoured
#: names it in ``required``, and :data:`HONOURED_REQUIRED` answers that.
_NEEDED_FIELDS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "verify": {"/context": _is_absolute_path, "/session": _is_present},
}


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """One JSON object, refusing the duplicate keys §5.2 calls invalid."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _Unusable("the request document has a duplicate key")
    return dict(pairs)


def _carries_null(value: Any) -> bool:
    """Whether *value* holds a ``null`` anywhere inside it.

    "``null`` never means 'absent'; it is invalid" (§5.2) — at any depth
    and in any field, including one this program would otherwise ignore.
    The rule governs the document, not the fields one program happens to
    read.
    """
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_carries_null(item) for item in value.values())
    if isinstance(value, list):
        return any(_carries_null(item) for item in value)
    return False


def _parse_request(path: str) -> dict[str, Any]:
    """The request document at *path*, or :class:`_Unusable`.

    The only program-caused error that cannot produce a result document
    (§5.1 step 4), and precisely the case in which the program does not
    know where a result would go. A byte-order mark is refused by the JSON
    parser itself, which is what §5.2's "UTF-8 without BOM" asks for.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as failure:
        raise _Unusable(f"the request document cannot be read: {failure}") from failure
    try:
        document = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except ValueError as failure:
        raise _Unusable(f"the request document is not JSON: {failure}") from failure
    if not isinstance(document, dict):
        raise _Unusable("the request document is not a JSON object")
    if _carries_null(document):
        raise _Unusable("the request document carries a null")
    return document


def _result_path(document: dict[str, Any]) -> Path:
    """Where the result document goes, from the immortal preamble.

    "From here on **every** error is a result document" (§5.1 step 5) —
    which is true exactly because this function refused everything that
    would have made that impossible.
    """
    value = document.get("result")
    if not _is_absolute_path(value):
        raise _Unusable("the request document names no absolute result path")
    return Path(value)


def _resolve_pointer(document: dict[str, Any], pointer: str) -> Any:
    """RFC 6901 evaluation of *pointer* against *document*.

    Returns :data:`_MISSING` for anything that does not resolve, an
    invalid pointer syntax included: §5.2 rule 2 asks whether the program
    "can honour the value it finds there", and finding nothing is one way
    of not being able to.
    """
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        return _MISSING
    current: Any = document
    for escaped in pointer.split("/")[1:]:
        token = escaped.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (token != "0" and token.startswith("0")):
                return _MISSING
            index = int(token)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _unhonourable(document: dict[str, Any]) -> list[str] | None:
    """The entries of ``required`` this program cannot honour (§5.2 rule 2).

    ``None`` means ``required`` is not a list of strings at all, which is
    a document this program cannot read as specified — rule 3, not rule 2,
    because there is no pointer to name in ``error.details.required``.
    An absent ``required`` is ``[]`` (§5.2), so it honours vacuously.
    """
    required = document.get("required", [])
    if not isinstance(required, list) or not all(isinstance(entry, str) for entry in required):
        return None
    offending: list[str] = []
    for pointer in required:
        honours = HONOURED_REQUIRED.get(pointer)
        if honours is None:
            offending.append(pointer)
            continue
        value = _resolve_pointer(document, pointer)
        if value is _MISSING or not honours(value):
            offending.append(pointer)
    return offending


def _not_found(action: str, document: dict[str, Any]) -> list[str]:
    """The fields *action* needs and this document does not supply (rule 3).

    Named as pointers rather than as field names so the refusal reads in
    the same vocabulary ``required`` does, and so a nested field can be
    named the day one is needed. The list is :data:`_NEEDED_FIELDS`, which
    is deliberately *not* §5.2's list of fields mandatory for a working
    action: rule 3 refuses over what the program needs, and §4.1 forbids
    requiring what it does not.
    """
    needed = _NEEDED_FIELDS.get(action, {})
    return [
        pointer
        for pointer, usable in needed.items()
        if not usable(_resolve_pointer(document, pointer))
    ]


def _relative_paths(document: dict[str, Any]) -> list[str]:
    """Every known path field of *document* whose value is not absolute.

    "Every path value is absolute" (§5.2) is stated of the document rather
    than of one action's fields, so every field the contract defines as a
    path is checked — including the ones ``describe`` has no use for. An
    unknown field is not checked, because rule 1 says to ignore it, and a
    known field holding something that is not a string is not checked
    either: that is not a path value at all, and no action implemented
    here needs one.
    """
    found: list[str] = []

    def check(name: str, value: Any) -> None:
        if isinstance(value, str) and not _is_absolute_path(value):
            found.append(name)

    for name in _PATH_FIELDS:
        check(name, document.get(name))
    for name in _PATH_OBJECTS:
        entry = document.get(name)
        if isinstance(entry, dict):
            check(f"{name}.path", entry.get("path"))
    for name in _PATH_OBJECT_MAPS:
        entries = document.get(name)
        if isinstance(entries, dict):
            for key, entry in entries.items():
                if isinstance(entry, dict):
                    check(f"{name}.{key}.path", entry.get("path"))
    return sorted(found)


# --------------------------------------------------------------------------
# The result document (§5.4)
# --------------------------------------------------------------------------


def _result_document(
    echo: dict[str, Any],
    status: str,
    *,
    reason: str | None = None,
    error: dict[str, Any] | None = None,
    context: str | None = None,
    program: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A result document in the field order §5.4 prints it in.

    *echo* is what the invocation was given and nothing else: ``action``
    always, because it is ``argv[1]``, and ``session`` iff the request
    document carried it. "A program MUST NOT invent a value for a field it
    was never given" (§5.4).

    *context* is passed exactly when the effective context ID was
    measured, which is what §5.4's "MUST, on success" row is about — see
    the module docstring. ``layers`` and ``artifacts`` never appear at
    all: the first is the "MUST NOT" row for both actions implemented
    here, and the second would declare output this program does not
    produce (``verify`` MAY declare diagnostic output; this one writes
    none, so there is nothing to declare).
    """
    result: dict[str, Any] = {"result": RESULT_VERSION, "status": status}
    result.update(echo)
    result["reason"] = reason
    result["error"] = error
    if context is not None:
        result["context"] = context
    if program is not None:
        result["program"] = program
    return result


def _refusal(
    echo: dict[str, Any],
    reason: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    program: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An ``unsupported`` result: exit 1, with a ``reason`` to match on.

    ``error.retryable`` is false for every refusal this program makes, and
    it is "the program's promise about its own failure, and about nothing
    else" (§5.4.1): re-running an identical request against an identical
    program produces the identical refusal. ``error.details`` is ``{}``
    wherever the contract fixes no contents for it — "Contract v1 fixes
    its contents only where a ``reason`` says so".
    """
    return _result_document(
        echo,
        _STATUS_UNSUPPORTED,
        reason=reason,
        error={"retryable": False, "message": message, "details": details or {}},
        program=program,
    )


def _failure(
    echo: dict[str, Any],
    reason: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    """A ``failure`` result: the work ran and did not succeed (§5.3).

    The other half of the "not ``success``" space, and the one §5.4's
    registry reserves its ``error.*`` reasons for. ``retryable`` is false
    for every failure this program produces, for the same reason it is
    false for every refusal: it is "the program's promise about its own
    failure, and about nothing else" (§5.4.1), and a context that
    disagrees with its own integrity list disagrees with it just as much
    on a second reading. Nothing here is a transient condition the program
    could wait out — the remedy is a different context, which is a
    different invocation.
    """
    return _result_document(
        echo,
        _STATUS_FAILURE,
        reason=reason,
        error={"retryable": False, "message": message, "details": details or {}},
        context=context,
    )


def _write_atomically(path: Path, document: dict[str, Any]) -> None:
    """Write *document* to *path* the way §5.4 prescribes.

    "temporary file in the *same* directory, ``fsync``, ``rename``" —
    same directory so the rename cannot cross a filesystem, ``fsync`` so
    the bytes are on disk before the name exists, rename because that is
    the one operation a reader cannot observe half of. A failure anywhere
    leaves neither a result document nor a temporary file behind, which is
    what makes "exit 66, nothing written" true of the write as well as of
    the parse.

    The file keeps :func:`tempfile.mkstemp`'s own mode. The contract says
    nothing about the result document's permissions, and the backend
    either runs the program as itself or outranks it.
    """
    payload = json.dumps(document, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# describe (§7.1)
# --------------------------------------------------------------------------


def _record_layers(record: Path) -> dict[str, Any]:
    """The ``layers`` block of the image's workspace record, or nothing.

    Unreadable and malformed are the same answer as absent, on purpose: a
    ``describe`` that cannot answer is a failed conformance test (§7.1),
    while a ``describe`` reporting ``"path": null`` asks the backend to
    supply the trees — which is the safe direction and the one every
    backend can satisfy.
    """
    try:
        content = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(content, dict):
        return {}
    layers = content.get("layers")
    return layers if isinstance(layers, dict) else {}


def trees(record: Path = WORKSPACE_RECORD) -> dict[str, dict[str, Any]]:
    """Where this program keeps each layer it carries (§7.1.1 ``trees``).

    Every layer of the contract's registry is reported, plus anything else
    the record names — an image that carries an ``x-`` layer says so. A
    ``path`` of ``null`` is the contract's own way of saying "not in my
    image", which is also what the record's ``mounted`` flag means: the
    SDK is a hash-pinned package fetched per session (ADR 0018), so the
    directory it lands in is the backend's to name, not this program's to
    report.

    ``version`` is the revision the record carries, and is omitted rather
    than guessed where there is none — §7.1.1 makes it optional for
    exactly that case.
    """
    layers = _record_layers(record)
    block: dict[str, dict[str, Any]] = {}
    for name in dict.fromkeys([*LAYERS, *sorted(layers)]):
        entry = layers.get(name)
        if not isinstance(entry, dict) or entry.get("mounted"):
            block[name] = {"path": None}
            continue
        path = entry.get("path")
        tree: dict[str, Any] = {"path": path if isinstance(path, str) else None}
        revision = entry.get("revision")
        if isinstance(revision, str):
            tree["version"] = revision
        block[name] = tree
    return block


def program(record: Path = WORKSPACE_RECORD) -> dict[str, Any]:
    """The self-description of §7.1.1, every field of it.

    ``version`` is the package's own and is opaque to a backend: "A
    backend MAY log it … it MUST NOT parse it and MUST NOT make a
    compatibility decision from it." Compatibility is decided by
    ``contract``, ``request``, ``result`` and ``actions``, which are
    declarations rather than inferences — and all four are constants here.
    """
    return {
        "id": PROGRAM_ID,
        "version": __version__,
        "contract": CONTRACT_VERSION,
        "request": list(REQUEST_VERSIONS),
        "result": list(RESULT_VERSIONS),
        "actions": list(IMPLEMENTED_ACTIONS),
        "trees": trees(record),
    }


def _describe(echo: dict[str, Any], record: Path) -> dict[str, Any]:
    """``describe``: read two fields, write four, plus ``program`` (§7.1).

    It "never touches the context, writes nothing but the result document,
    and fills the ``program`` block", so there is no context ID to report
    and nothing measured to declare.
    """
    return _result_document(echo, _STATUS_SUCCESS, program=program(record))


# --------------------------------------------------------------------------
# verify (§7.3)
# --------------------------------------------------------------------------


def _reportable(value: Any) -> Any:
    """*value* as something :func:`json.dumps` can write.

    The declared ``context`` format version reaches ``error.details``
    straight out of a YAML document, where a scalar can parse as a date,
    a mapping or anything else. The result document is the last write
    action of the invocation (§5.4), so a value the JSON encoder chokes on
    there would cost the whole invocation its answer — nothing written and
    exit 66, for a context this program diagnosed perfectly well. Carrying
    the value as text is the smaller loss, and the field exists so a
    backend can see what it sent.
    """
    return value if isinstance(value, bool | int | str) else str(value)


def _verify(echo: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    """``verify``: the materialized context against its own integrity list.

    §7.3: "Asserts that the materialized context is the context the
    manifest describes. It checks the **effective** context — the file set
    as materialized, against the integrity list in ``manifest.yaml`` … —
    and reports the resulting ``context`` ID in its result. A file that is
    missing, a file whose bytes hash to something else, and a file present
    but absent from the list are one outcome and one typed answer:
    ``status: "failure"``, ``reason: "error.context.mismatch"``, the
    offending paths in ``error.details``."

    Every hash and the ID itself come from
    :func:`~mcuhome.contextdir.verify_context`, never from this module —
    see the module docstring for why a second implementation of §3.3 here
    would be the defect that rule exists against. Its
    :attr:`~mcuhome.contextdir.ContextVerification.ok` covers one case
    §7.3 does not enumerate and §3.3 demands anyway: a manifest whose
    declared ``id`` is not the ID its own contents yield. "Implementations
    … MUST NOT trust a declared ``id`` value" — and a declared value
    nobody checks is one nothing in the system would ever catch, since
    every other party recomputes and would agree with itself.

    **This invocation writes nothing but its result document.** §9.2 point
    10 forbids a ``verify`` to "Apply a patch, write into a ``trees``
    entry, or write into ``work``"; this one needs none of those paths at
    all. No event is written either: ``events`` is optional in both
    directions (§8) and "a program that offers fewer names than the table
    is conforming". ``cancel`` is not polled, which §8 leaves as a SHOULD
    "so that a fifty-line third-party program stays possible" — this
    action is one pass over one directory, and the backend's SIGTERM
    remains the hard path.
    """
    root = Path(document["context"])
    if not (root / MANIFEST_FILE).is_file():
        # "is missing a file the action needs … the missing path in
        # error.details" (§5.4). §3.1 makes this *the* file: "manifest.yaml
        # is the program's entry point; a program MUST NOT require any
        # out-of-band knowledge beyond it and this contract." A context
        # directory that is not there at all lands here too, which is
        # right — from the program's side the two are the same absence.
        return _failure(
            echo,
            _REASON_INCOMPLETE,
            f"the context at {root} carries no {MANIFEST_FILE}",
            details={"missing": [MANIFEST_FILE]},
        )

    try:
        verification = verify_context(root)
    except ContextFormatVersionError as unimplemented:
        if unimplemented.found is None:
            return _failure(
                echo,
                _REASON_MISMATCH,
                f"the context at {root} states no {MANIFEST_FILE} format version",
                details={"paths": []},
            )
        return _refusal(
            echo,
            _REASON_CONTEXT,
            unimplemented.message,
            details={"context": _reportable(unimplemented.found)},
        )
    except (BuildError, OSError) as unreadable:
        # A manifest that parses as nothing, states a hash in a spelling
        # §3.3.1 refuses, or a file that cannot be read at all. The
        # contract types none of these; see the module docstring.
        detail = unreadable.message if isinstance(unreadable, BuildError) else str(unreadable)
        return _failure(
            echo,
            _REASON_MISMATCH,
            f"the context at {root} cannot be read as one: {detail}",
            details={"paths": []},
        )

    if not verification.ok:
        return _failure(
            echo,
            _REASON_MISMATCH,
            "; ".join(verification.problems()),
            details={"paths": [mismatch.path for mismatch in verification.mismatches]},
            # Measured, so reported — the module docstring quotes the line.
            context=verification.actual_id,
        )
    return _result_document(echo, _STATUS_SUCCESS, context=verification.actual_id)


# --------------------------------------------------------------------------
# The invocation (§5.1)
# --------------------------------------------------------------------------


def _invoke(action: str, document: dict[str, Any], record: Path) -> dict[str, Any]:
    """One invocation, in the order of §5.1's bootstrap chain.

    §5.4's table makes ``program`` mandatory in a ``describe`` result and
    qualifies the row with nothing — "Everything not qualified is
    mandatory unconditionally" — so a ``describe`` that *refuses* carries
    the block too. Nothing is fabricated by doing so: the block is static
    self-description, and a refusal is exactly the moment a backend needs
    it, since ``program.request`` is what tells it which request format
    version to send instead. In a ``verify`` or ``build`` result the block
    is a MAY, and this program omits it there.
    """
    echo: dict[str, Any] = {"action": action}
    if "session" in document:
        # The echo rule (§5.4): whatever the request carried, verbatim.
        # ``session`` is an opaque token; nothing here composes a path from
        # it, so its type never has to be believed.
        echo["session"] = document["session"]
    block = program(record) if action == "describe" else None

    def refuse(reason: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        return _refusal(echo, reason, message, details=details, program=block)

    if not _is_request_version(document.get("request")):
        return refuse(
            _REASON_REQUEST,
            f"request format version {document.get('request')!r} is not implemented; "
            f"this program parses {list(REQUEST_VERSIONS)}",
        )

    if action not in IMPLEMENTED_ACTIONS:
        return refuse(
            _REASON_ACTION,
            f"action {action!r} is not implemented; this program implements "
            f"{list(IMPLEMENTED_ACTIONS)}",
        )

    offending = _unhonourable(document)
    if offending is None:
        return refuse(_REASON_REQUEST, "'required' is not an array of JSON Pointers")
    if offending:
        return refuse(
            _REASON_REQUIRED,
            f"this program does not honour {', '.join(offending)} with the value given",
            details={"required": offending},
        )

    relative = _relative_paths(document)
    if relative:
        return refuse(
            _REASON_REQUEST,
            f"every path value is absolute; these are not: {', '.join(relative)}",
        )

    unfound = _not_found(action, document)
    if unfound:
        return refuse(
            _REASON_REQUEST,
            f"{action!r} needs {', '.join(unfound)}, and this request document "
            f"supplies no usable value there",
        )

    if action == "verify":
        return _verify(echo, document)
    return _describe(echo, record)


def main(argv: list[str], *, record: Path = WORKSPACE_RECORD) -> int:
    """One invocation of the program; the return value is its exit code.

    *argv* is the whole command line, program name included, exactly as a
    launcher hands ``sys.argv`` over: ``argv[1]`` is the action and
    ``argv[2]`` the absolute path of the request document. "Exactly two
    positional operands, both mandatory, **never a flag**. Any other arity
    is exit 66" (§5.1) — so there is no option parser here and there never
    will be one, because the argv is frozen and extensibility runs through
    the request document alone.

    *record* is where the image's workspace record is looked for, and is a
    parameter only so a test can point it somewhere. Nothing on the
    command line moves it: it is a property of the image, not of an
    invocation.

    **A relative ``argv[2]`` is exit 66**, alongside the wrong arity.
    §5.1 states the operand as "<absolute path of the request document>"
    and then forbids the only thing that could make a relative one
    meaningful: "the working directory is meaningless: a program MUST NOT
    rely on any ``cwd``". Reading it anyway is the one failure this
    module could have that nobody would see — the path resolves against
    whatever directory the backend happened to leave the process in, so
    it either finds nothing or, worse, finds a *different* request
    document and answers that one. Refusing costs a conforming backend
    nothing, because a conforming backend never sends one.
    """
    if len(argv) != 3:
        return EXIT_UNUSABLE
    action, request = argv[1], argv[2]
    if not _is_absolute_path(request):
        return EXIT_UNUSABLE

    try:
        document = _parse_request(request)
        destination = _result_path(document)
    except _Unusable:
        return EXIT_UNUSABLE

    result = _invoke(action, document, record)

    try:
        _write_atomically(destination, result)
    except OSError:
        return EXIT_UNUSABLE

    return EXIT_SUCCESS if result["status"] == _STATUS_SUCCESS else EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover - the launcher's entry point
    import sys

    raise SystemExit(main(sys.argv))
