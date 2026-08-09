# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The invocation ABI of the build container contract, and ``describe``.

``docs/design/build-container-contract.md`` §5 is frozen. A backend runs

    /mcuhome/run <action> <absolute path of the request document>

and reads a JSON result document back from the path the request named.
This module is that ABI: argv, the request document's five parsing rules
(§5.2), the three exit codes (§5.3), and the atomic result document
(§5.4). ``containers/builder/run`` is the thin launcher the image
installs at that fixed path; everything it does is call :func:`main`.

**One action is implemented: ``describe`` (§7.1).** ``build`` and
``verify`` are not, and they refuse the way §7 says an unimplemented
action refuses — ``status: "unsupported"``, ``reason:
"unsupported.action"``, exit 1 — which is a legible answer a backend can
reschedule on rather than a crash.

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

*``program.actions`` lists ``describe`` alone.* §7.1.1 calls it "the
action names the program implements" and in the same breath requires it
to "include ``describe``, ``verify`` and ``build``". Both cannot hold for
a program that implements one of the three. Listing an action that
answers ``unsupported.action`` would be the worse lie of the two — a
backend MUST NOT invoke what is absent from the list, so the honest list
costs a refusal nobody has to reschedule. This is also why the image
carries no ``org.mcuhome.contract`` label yet: it is not a conforming
image, and it does not claim to be.

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
#: ``program.actions``. See the module docstring for why ``build`` and
#: ``verify`` are not in it.
IMPLEMENTED_ACTIONS = ("describe",)

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
_STATUS_UNSUPPORTED = "unsupported"

_REASON_REQUEST = "unsupported.request"
_REASON_REQUIRED = "unsupported.required"
_REASON_ACTION = "unsupported.action"

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
#: Three, and no more. ``/request`` and ``/result`` are the immortal
#: preamble and this program acts on both; ``/session`` it echoes, which
#: is the whole of what §5.2 permits anyone to do with it. Every other
#: pointer — ``/out``, ``/work``, ``/context``, ``/trees/sdk``,
#: ``/limits/jobs``, ``/params/mode`` — belongs to a working action this
#: program does not implement, so a backend that demands it is told so
#: instead of being quietly served something else.
HONOURED_REQUIRED: dict[str, Callable[[Any], bool]] = {
    "/request": _is_request_version,
    "/result": _is_absolute_path,
    "/session": lambda value: isinstance(value, str),
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
    program: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A result document in the field order §5.4 prints it in.

    *echo* is what the invocation was given and nothing else: ``action``
    always, because it is ``argv[1]``, and ``session`` iff the request
    document carried it. "A program MUST NOT invent a value for a field it
    was never given" (§5.4).

    ``context``, ``layers`` and ``artifacts`` never appear. They are the
    "MUST NOT" row of §5.4's table for ``describe``, and for the two
    actions this program refuses they would report work that did not
    happen.
    """
    result: dict[str, Any] = {"result": RESULT_VERSION, "status": status}
    result.update(echo)
    result["reason"] = reason
    result["error"] = error
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
