# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The invocation ABI of the build container contract, and its three actions.

``docs/design/build-container-contract.md`` §5 is frozen. A backend runs

    /mcuhome/run <action> <absolute path of the request document>

and reads a JSON result document back from the path the request named.
This module is that ABI: argv, the request document's five parsing rules
(§5.2), the three exit codes (§5.3), and the atomic result document
(§5.4). ``containers/builder/run`` is the thin launcher the image
installs at that fixed path; everything it does is call :func:`main`.

**All three actions of contract v1 are implemented:** ``describe``
(§7.1), ``build`` (§7.2) and ``verify`` (§7.3). ``generate`` is not one
of them and never will be — §6.1 makes it "an action of the SDK entry
point and **not** of the program: it is never invoked on ``/mcuhome/run``
and never appears in ``program.actions``" — so it refuses the way §7 says
an unimplemented action refuses: ``status: "unsupported"``, ``reason:
"unsupported.action"``, exit 1, a legible answer a backend can reschedule
on rather than a crash.

``verify`` computes nothing itself. Every hash and the effective context
ID come from :func:`~mcuhome.workbench.contextdir.verify_context`, the one
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
Both can hold now, and the list stays the honest one either way: it is
what :data:`IMPLEMENTED_ACTIONS` says, never a constant copied out of the
contract. A backend MUST NOT invoke what is absent from the list, so a
list that ran ahead of the code would be the one lie a backend acts on.

*What each action needs out of the request document.* §5.2 makes seven
fields mandatory for every working action, and rule 3 refuses over a
narrower thing: "A field the program needs for this action and does not
find". ``verify`` needs two of the seven. ``context`` is the directory
§7.3 defines the action over. ``session`` is needed because §5.4 has a
``verify`` result carry it "in every conforming invocation" — the row is
conditional on §5.2 making the request carry it, so a request without it
is the backend's breach of §5.2, and refusing under rule 3 is how this
program reports a breach it will not paper over by inventing the echo.
The other five (``out``, ``work``, ``tmp``, ``trees.sdk``,
``limits.jobs``) name work this action does not do: it "reads the context
and nothing else" (§7.3), and §4.1 says "The program MUST NOT require an
entry it does not need for the requested action". A backend that omits
them is in breach of §5.2, but that is a defect in the backend's request
and not in this invocation's answer, and this program is not the thing
that reports it. ``build`` needs all seven, and acts on every one of
them, which is why :data:`HONOURED_REQUIRED` is a table per action rather
than one list: the same pointer is honourable for one action and a lie
for another.

*A manifest that cannot be read at all is ``error.context.unreadable``.*
"found ``manifest.yaml`` and cannot read it as one: broken YAML, a
missing section, or a hash in a spelling §3.3.1 refuses" — the reason
§5.4's registry provides, distinct from ``error.context.mismatch`` so a
backend can tell a corrupt manifest from a tampered context file by
``reason`` alone. This implementation once answered ``mismatch`` here
because the registry had no better value; the gap was recorded, taken to
the product owner, and closed as an erratum (E36) — which is the working
order of this contract: the reference implementation surfaces the gap,
the registry gains the value, the implementation follows the registry.

*A manifest that states no format version is unreadable too, not
``unsupported.context``.* That reason means the program "found a
``context`` format version it does not implement", and §3.2 explains the
status by "nothing about this context is broken". A manifest with no
``context`` key is broken — §7.3 now says it in as many words: "no other
image would fare better with it, so there is nothing for a backend to
reschedule onto".

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

**Where §7.2 is silent, and what ``build`` does instead.** Every entry
below is a decision this module took, not a rule it found. Each names the
line it stands on.

*A ``params.mode`` value this program does not implement is
``unsupported.required``.* §5.2 ("`required` and value granularity")
already says so — "A program that knows ``/params/mode`` but not the
value ``reproducible`` MUST refuse with ``unsupported.required``" — so
the bare case answers the same reason a value NAMED in ``required``
earns through :data:`_HONOURED_REQUIRED`. It is not
``unsupported.request``: the field is present and well-formed, and
refusing it as "missing" would misname a present-but-unimplemented
value. Silently executing ``reproducible`` as ``clean`` stays forbidden
— the §5.2 failure "accept the job and quietly deliver something else".

*The generated application tree and the CMake tree live in ``work``.*
§5.2 calls ``work`` "the session's persistent working area" and §7.2
defines ``incremental`` as "an ``incremental`` for which the program finds
no prior state of *this session* in ``work`` is executed as ``clean``".
A tree in ``tmp`` is gone every invocation, so ``mode`` could not mean
anything at all; ``work`` is the only place in the request document where
it can. ``clean`` deletes both trees and passes ``--pristine always``;
``incremental`` keeps them and lets
:func:`~mcuhome.compiler.workspace.pristine_mode` decide. The contract defines
``clean`` as "fresh workspace" and nothing more.

*This program writes a session marker, so that ``incremental`` has a
predicate.* §6.3 makes the marker optional and says an absent one "means
nothing" — which leaves a marker-less program with no way to evaluate
§7.2's "prior state of *this session*". Writing one is the only reading
under which the mode parameter is implementable, and §6.3 permits it
outright. It is therefore read on every invocation, before anything in
``work`` is touched, as §6.3 requires of a program that writes one.

*A marker that cannot be read is treated as foreign.* §6.3 types a marker
"naming a different session" and nothing else. This program writes its
own marker atomically, so a marker it cannot parse is not one it wrote —
and §6.3 forbids using, deleting or overwriting state it cannot claim.

*A context whose materialized files disagree with ``manifest.yaml`` does
not stop a ``build``.* §7.3 makes that ``verify``'s failure, and §5.4
makes ``result.context`` "the effective context ID actually worked on …
computed by the program from the context as materialized. It exists **for
comparison only**". So the build proceeds and reports what it measured;
the backend compares that against its own ID (§9.3) and is the party that
decides. A ``verify`` is one invocation away for a backend that wants the
check first.

*The key names inside ``error.details`` for the build reasons.* The
contract fixes only ``error.details.required`` and otherwise names facts.
So: ``missing`` carries "the missing path" (§7.2), ``layer`` names the
layer for ``error.layer.unknown`` and ``error.patch.incomplete``, and
``error.work.foreign`` carries "the two session IDs" as ``session`` (the
one this invocation was given) and ``found`` (the one in ``work``).

*Two artifacts carry role ``firmware``, and §7.2.1 says so.* "The
parameters apply to **every** artifact declared with role ``firmware``.
There is one unsigned image, and a build may declare it in more than one
encoding" — hex to flash, bin to sign, the same four ``imgtool``
arguments describing each. The section once said "the artifact",
singular, against §7.2's own two files; closed as an erratum (E36/D2).

*The bootloader is declared, and sysbuild's combined hex is not.* §7.2
requires "at least two" artifacts and makes only ``firmware`` and
``report`` mandatory. ``bootloader`` is measured output a client needs to
bring a fresh device up, and it is in §5.4's role registry. The combined
hex is not declared and never reaches ``out`` at all: on a build that
never signs it is the *unsigned* application under a name that looks
flashable, which is the hazard ``cli/mcuhome_cli/cli.py`` deletes it for.
Nothing in ``out`` is undeclared, so there is nothing to delete.

*``build.image.started`` and cancellation are not implemented.* §8 makes
both optional — "a program that offers fewer names than the table is
conforming", and cancellation is a SHOULD "so that a fifty-line
third-party program stays possible". ``/cancel`` is therefore **not** an
honoured pointer, and a backend that demands it is told so.
``limits.deadline_seconds`` is likewise not honoured: it is advisory,
"enforcement is the backend's", and ``error.deadline.exceeded`` never
occurs here.

**And the one thing that is a refusal rather than a decision.** §6.1
assigns the program the west workspace and names no mechanism for
pointing an existing one at a ``trees.<layer>.path`` the backend chose:
west resolves project paths from ``.west/config`` plus the manifest, and
neither the contract nor this repository has a way to move them at
invocation time. So :func:`_workspace` accepts a ``trees`` entry only
where it names the path the image's own record already has for that layer
— which is what a backend mounting a writable view *over* the baked tree
produces, and what ``containers/builder/Dockerfile`` already anticipates
("a file at the topdir would be shadowed the moment a backend bind-mounts
a writable view over it"). Anything else fails the invocation with
``error.build.failed``, naming the layer and both paths, rather than
building against a tree the backend did not name. What would replace it
is a west re-registration step — a manifest rewrite, or ``west config``
per project — and that is a design decision, not a translation.

This module reads no process state: ``argv`` arrives as an argument, and
every path it touches comes out of the request document. The environment
its **children** get is built here and passed to them, which is a
different thing from reading one — and it is built from nothing, exactly
as :func:`~mcuhome.compiler.container.container_environment` does for the other
direction. That is what keeps this module off the exemption list of
``tests_py/test_userpaths.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcuhome.compiler import report, workspace
from mcuhome.compiler.generate import APP_DIR
from mcuhome.model import __version__, registry
from mcuhome.model.context import MANIFEST_FILE, MODEL_FILE, PATCHES_DIR
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file
from mcuhome.model.modelfile import read_model

# mcuhome.workbench.contextdir is imported inside _open_context, not
# here: the SDK entry point (§6.1) imports this module for
# run_invocation, and its runtime is what the SDK package declares — a
# bare interpreter, no third-party packages. contextdir carries the YAML
# emitter and pulls ruamel at import, which only the program's own
# environment provides. tests_py/test_container_closure.py pins the
# entry point's closure to stdlib plus mcuhome.
if TYPE_CHECKING:
    from mcuhome.workbench.contextdir import ContextVerification

__all__ = [
    "BOOTLOADER_ARTIFACT",
    "CONTRACT_VERSION",
    "EXIT_FAILURE",
    "EXIT_SUCCESS",
    "EXIT_UNUSABLE",
    "FIRMWARE_ARTIFACTS",
    "IMPLEMENTED_ACTIONS",
    "LAYERS",
    "MODES",
    "PROGRAM_ID",
    "REPORT_ARTIFACT",
    "REPORT_VERSION",
    "REQUEST_VERSIONS",
    "RESULT_VERSION",
    "RESULT_VERSIONS",
    "SDK_METADATA_FILE",
    "SDK_METADATA_VERSIONS",
    "SIGNING_KEY_FILE",
    "WORKSPACE_RECORD",
    "honoured_required",
    "main",
    "run_invocation",
    "patchset",
    "program",
    "sdk_entry_point",
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
#: ``program.actions``. Contract v1 defines exactly these three;
#: ``generate`` is deliberately absent (§6.1).
IMPLEMENTED_ACTIONS = ("describe", "verify", "build")

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
# What a `build` is made of (§7.2)
# --------------------------------------------------------------------------

#: The two ``params.mode`` values §7.2 defines, and the default §5.2
#: fixes: "an absent ``params``, a ``params`` object without a ``mode``
#: key, and ``params: {}`` are the same thing and all three mean
#: ``mode: "clean"``".
MODES = ("clean", "incremental")
DEFAULT_MODE = MODES[0]

#: The bootloader's verification key inside the context. Mandatory for
#: ``build`` and for ``build`` alone (§7.2), with no fallback: MCUboot's
#: own default is a demo key whose private half is published.
SIGNING_KEY_FILE = "keys/signing.pub"

#: Where the SDK package declares its code-generation entry point, at the
#: root of ``trees.sdk`` (§6.1, normative). Contract v1 fixes the file
#: name and three field names — ``sdk``, ``generate.program``,
#: ``generate.runtime`` — and no values.
SDK_METADATA_FILE = "mcuhome-sdk.json"

#: ``sdk`` metadata format versions this program implements. A version
#: outside it is ``error.build.failed`` and never ``unsupported``: "the
#: program implements everything this contract asks of it, and no other
#: container would fare better with this SDK package" (§6.1).
SDK_METADATA_VERSIONS = (1,)

#: The action the SDK entry point is invoked with (§6.1). Never an action
#: of *this* program.
GENERATE_ACTION = "generate"

#: What the program keeps inside ``work`` — the session's persistent area
#: (§4). Every name is this program's own; the contract fixes none of
#: them, and nothing outside this module may depend on them.
WORK_MARKER = "session.json"
WORK_PATCH_RECORDS = "patches"
WORK_TREE = "tree"
WORK_BUILD = "build"
WORK_CCACHE = "ccache"
WORK_HOME = "home"
#: The writable copy of the workspace's ``.west/config`` (see
#: :meth:`_Invocation._environment`).
WORK_WEST_CONFIG = "west-config"
#: ``XDG_CACHE_HOME`` for the build's children (see
#: :meth:`_Invocation._environment`).
WORK_XDG_CACHE = "cache"

#: ``<sysbuild artifact> -> <name in out>`` for the unsigned application
#: image, whose role is ``firmware``. §7.2: "MCUHome's own container
#: writes ``firmware.hex`` and ``firmware.bin``".
FIRMWARE_ARTIFACTS = (("zephyr.hex", "firmware.hex"), ("zephyr.bin", "firmware.bin"))

#: The same for MCUboot, whose role is ``bootloader``. Not required by
#: §7.2 and declared anyway — see the module docstring.
BOOTLOADER_ARTIFACT = ("zephyr.hex", "bootloader.hex")

#: The mandatory ``report`` artifact (§7.2, §7.2.1), and the format
#: version this module writes. "A consumer that does not implement the
#: version it finds MUST NOT sign from the document."
REPORT_ARTIFACT = "build-report.json"
REPORT_VERSION = 1

#: The prefix of §5.4's ``layers[<name>].patchset`` encoding, stated by
#: the contract as a literal and therefore never composed here.
_PATCHSET_PREFIX = "mcuhome-patchset-1\n"

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
_REASON_UNREADABLE = "error.context.unreadable"
_REASON_LAYER = "error.layer.unknown"
_REASON_PATCH = "error.patch.incomplete"
_REASON_WORK = "error.work.foreign"
_REASON_BUILD = "error.build.failed"
_REASON_INTERNAL = "error.internal"

# --------------------------------------------------------------------------
# The request document (§5.2)
# --------------------------------------------------------------------------


class _Unusable(Exception):
    """No result can be addressed: exit 66, nothing written (§5.3).

    Not an error type from :mod:`mcuhome.model.errors`, on purpose. Those render
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


def _is_present(value: Any) -> bool:
    """Anything at all, as opposed to :data:`_MISSING`."""
    return value is not _MISSING


def _is_jobs(value: Any) -> bool:
    """``limits.jobs`` as §5.2 defines it: authoritative, so a real count.

    ``bool`` is excluded for the reason :func:`_is_request_version` gives.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_tree_entry(value: Any) -> bool:
    """One ``trees`` entry: an object with an absolute ``path`` (§4.1).

    ``writable`` is not checked here, and deliberately not: it is
    "asserted by the backend, never probed by the program", so a value
    this program cannot verify is not one it can promise to honour by
    inspecting it. What it promises is to *read* the flag rather than to
    test it, which is what §6.2 asks for.
    """
    return isinstance(value, dict) and _is_absolute_path(value.get("path"))


def _is_mode(value: Any) -> bool:
    """A ``params.mode`` value §7.2 defines. Absent is ``clean`` (§5.2)."""
    return value is _MISSING or value in MODES


#: The JSON Pointers this program honours **per action**, each with the
#: values it can honour there (§5.2 rule 2: "knowing the path is not
#: enough").
#:
#: Per action, because the same pointer is a promise for one action and a
#: lie for another: a ``verify`` "reads the context and nothing else"
#: (§7.3) and does nothing whatever with ``/out``, ``/work``, ``/tmp``,
#: ``/trees/sdk`` or ``/limits/jobs``, although §5.2 makes all five
#: mandatory in the request it arrives in — while a ``build`` writes into
#: three of them and takes its parallelism from the fourth. Promising to
#: honour a value nothing reads is the cheapest form of the lie §5.2
#: names: "accept the job and quietly deliver something else".
#:
#: The preamble is honoured by every action, and each entry for its own
#: reason: ``/request`` because a version outside :data:`REQUEST_VERSIONS`
#: is refused rather than parsed hopefully; ``/result`` because the result
#: document is written to exactly that path; ``/session`` because it is
#: echoed, which is the whole of what §5.2 permits anyone to do with it,
#: and honourable only as the opaque *string* token the contract defines.
#:
#: Two pointers a conforming ``build`` request may carry are deliberately
#: **absent**: ``/cancel``, because cancellation is a SHOULD this program
#: does not implement (§8), and ``/limits/deadline_seconds``, because it
#: is advisory and enforcement is the backend's (§5.2). A backend that
#: demands either is told which pointer failed.
_PREAMBLE_HONOURED: dict[str, Callable[[Any], bool]] = {
    "/request": _is_request_version,
    "/result": _is_absolute_path,
    "/session": lambda value: isinstance(value, str),
}

#: The part of the table that is a property of this *program*. What it
#: honours for ``/trees/<layer>`` is a property of the *image* instead,
#: and :func:`honoured_required` is where the two are put together.
_HONOURED_REQUIRED: dict[str, dict[str, Callable[[Any], bool]]] = {
    "describe": dict(_PREAMBLE_HONOURED),
    "verify": {**_PREAMBLE_HONOURED, "/context": _is_absolute_path},
    "build": {
        **_PREAMBLE_HONOURED,
        "/context": _is_absolute_path,
        "/out": _is_absolute_path,
        "/work": _is_absolute_path,
        "/tmp": _is_absolute_path,
        "/events": _is_absolute_path,
        "/ccache": _is_tree_entry,
        "/limits/jobs": _is_jobs,
        "/params/mode": _is_mode,
    },
}


def _tree_at(expected: Path | None) -> Callable[[Any], bool]:
    """Honours a ``trees`` entry naming *expected*, and no other path.

    ``None`` honours nothing: a layer this program has no tree for is a
    layer no value of ``/trees/<layer>`` can be honoured for.
    """

    def honours(value: Any) -> bool:
        if expected is None or not _is_tree_entry(value):
            return False
        return Path(value["path"]) == expected

    return honours


def honoured_required(
    action: str, record: Path = WORKSPACE_RECORD
) -> dict[str, Callable[[Any], bool]]:
    """The pointers *action* honours, with the values it can honour there.

    §5.2 rule 2, per action, because the same pointer is a promise for one
    action and a lie for another: a ``verify`` "reads the context and
    nothing else" (§7.3) and does nothing whatever with ``/out``,
    ``/work``, ``/tmp``, ``/trees/sdk`` or ``/limits/jobs``, although §5.2
    makes all five mandatory in the request it arrives in — while a
    ``build`` writes into three of them and takes its parallelism from the
    fourth. Promising to honour a value nothing reads is the cheapest form
    of the lie §5.2 names: "accept the job and quietly deliver something
    else".

    **``/trees/<layer>`` is honoured for exactly one value per layer**,
    which is why the table needs the image's record and cannot be a
    constant. "Knowing the path is not enough: it must be able to honour
    the value it finds there" — and :meth:`_Build._workspace` can honour
    exactly the path the record already has for that layer, because west
    resolves project paths from ``.west/config`` plus the manifest and
    nothing here moves them at invocation time. A backend that names any
    other path in ``required`` is therefore told ``unsupported.required``,
    which is what §5.2 rule 2 mandates, rather than being served
    ``error.build.failed`` from the middle of the build — the same fact,
    reported in the channel the backend asked in.

    A layer the record does not name is honoured for nothing at all, and
    that includes every layer when there is no record: a program with no
    workspace of its own cannot promise to build against any tree.
    """
    table = dict(_HONOURED_REQUIRED.get(action, {}))
    if action == "build":
        paths = _record_tree_paths(record)
        for layer in dict.fromkeys([*LAYERS, *sorted(paths)]):
            table[f"/trees/{layer}"] = _tree_at(paths.get(layer))
    return table


#: What each action needs to find in the request document, as a JSON
#: Pointer and the values that are usable there. Rule 3 of §5.2: "A field
#: the program needs for this action and does not find … ⇒ ``status:
#: "unsupported"``, ``reason: "unsupported.request"``".
#:
#: ``describe`` "needs only the preamble" (§5.2) and so is absent here.
#: ``verify`` needs two of the seven fields §5.2 makes mandatory for a
#: working action, and the module docstring says why the other five are
#: not demanded back. ``build`` needs all seven, because it acts on all
#: seven. ``/session`` is checked for presence and not for type: §5.4's
#: echo rule says a program echoes what it was given, verbatim, and §5.2
#: forbids composing a path from it, so its type never has to be believed.
#: A backend that wants the token's type honoured names it in
#: ``required``, and :data:`HONOURED_REQUIRED` answers that.
#:
#: ``/params/mode`` is here rather than only in the honoured table
#: ``/params/mode`` is not here: a missing mode is usable (it means
#: ``clean``), so the only way it could fail this presence check is a
#: value like ``reproducible`` — and §5.2 ("`required` and value
#: granularity") already fixes what that is: "A program that knows
#: ``/params/mode`` but not the value ``reproducible`` MUST refuse with
#: ``unsupported.required`` rather than accept the job." That is a
#: present field with an unimplemented value, not a missing one, so
#: :func:`_unsupported_mode` handles it apart from rule 3's missing
#: fields.
_NEEDED_FIELDS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "verify": {"/context": _is_absolute_path, "/session": _is_present},
    "build": {
        "/context": _is_absolute_path,
        "/session": _is_present,
        "/out": _is_absolute_path,
        "/work": _is_absolute_path,
        "/tmp": _is_absolute_path,
        "/trees/sdk/path": _is_absolute_path,
        "/limits/jobs": _is_jobs,
    },
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


def _unhonourable(action: str, document: dict[str, Any], record: Path) -> list[str] | None:
    """The entries of ``required`` *action* cannot honour (§5.2 rule 2).

    ``None`` means ``required`` is not a list of strings at all, which is
    a document this program cannot read as specified — rule 3, not rule 2,
    because there is no pointer to name in ``error.details.required``.
    An absent ``required`` is ``[]`` (§5.2), so it honours vacuously.
    """
    required = document.get("required", [])
    if not isinstance(required, list) or not all(isinstance(entry, str) for entry in required):
        return None
    honoured = honoured_required(action, record)
    offending: list[str] = []
    for pointer in required:
        honours = honoured.get(pointer)
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


def _unsupported_mode(action: str, document: dict[str, Any]) -> Any:
    """A present ``/params/mode`` this program does not implement, or None.

    §7.2 enumerates ``clean`` and ``incremental``. A third value is one
    the program cannot honour, which §5.2 already scopes to
    ``unsupported.required`` ("A program that knows ``/params/mode`` but
    not the value ``reproducible`` MUST refuse with
    ``unsupported.required``") — distinct from a *missing* mandatory
    field (``unsupported.request``), because the field is present and
    well-formed; it is its value the program does not implement. An
    absent mode is ``clean`` (§5.2) and usable, so it is never one of
    these.
    """
    if action != "build":
        return None
    value = _resolve_pointer(document, "/params/mode")
    if value is _MISSING or value in MODES:
        return None
    return value


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
    artifacts: list[dict[str, Any]] | None = None,
    layers: dict[str, Any] | None = None,
    program: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A result document in the field order §5.4 prints it in.

    *echo* is what the invocation was given and nothing else: ``action``
    always, because it is ``argv[1]``, and ``session`` iff the request
    document carried it. "A program MUST NOT invent a value for a field it
    was never given" (§5.4).

    Every optional field below is passed exactly when the invocation
    *measured* the thing it reports, which is what §5.4's "MUST, on
    success" rows are about: "An invocation that failed before it got that
    far reports what it measured and nothing more … Fabricating either
    would be worse than omitting it, since the backend compares both
    against its own values." So *context* appears once the effective ID
    has been computed, and *artifacts* and *layers* only on a successful
    ``build`` — for ``describe`` and ``verify`` they are the table's "MUST
    NOT" rows, and a ``verify`` that declared diagnostic output (which it
    MAY) would still declare no ``layers``, because "it reports work that
    was actually done, and ``verify`` does not do that work".
    """
    result: dict[str, Any] = {"result": RESULT_VERSION, "status": status}
    result.update(echo)
    result["reason"] = reason
    result["error"] = error
    if context is not None:
        result["context"] = context
    if artifacts is not None:
        result["artifacts"] = artifacts
    if layers is not None:
        result["layers"] = layers
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


def _record_document(record: Path) -> dict[str, Any]:
    """The image's workspace record, or an empty document.

    Unreadable and malformed are the same answer as absent, on purpose: a
    ``describe`` that cannot answer is a failed conformance test (§7.1),
    while a ``describe`` reporting ``"path": null`` asks the backend to
    supply the trees — which is the safe direction and the one every
    backend can satisfy. A ``build`` reads the same document and cannot
    be so relaxed about it (:func:`_workspace`), because a program with no
    workspace of its own has no build environment to assemble.
    """
    try:
        content = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return content if isinstance(content, dict) else {}


def _record_layers(record: Path) -> dict[str, Any]:
    """The ``layers`` block of the image's workspace record, or nothing."""
    layers = _record_document(record).get("layers")
    return layers if isinstance(layers, dict) else {}


def _record_tree_paths(record: Path) -> dict[str, Path]:
    """``<layer> -> <where this program builds it>``, from the record.

    The one answer to "which path can this program honour for this
    layer", read by :func:`honoured_required` before a build starts and by
    :meth:`_Build._workspace` while it runs. A layer whose entry names no
    string path is absent from the result rather than present with a
    guess.
    """
    return {
        name: Path(entry["path"])
        for name, entry in _record_layers(record).items()
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }


def trees(record: Path = WORKSPACE_RECORD) -> dict[str, dict[str, Any]]:
    """Where this program keeps each layer it carries (§7.1.1 ``trees``).

    Every layer of the contract's registry is reported, plus anything else
    the record names — an image that carries an ``x-`` layer says so. A
    ``path`` of ``null`` is the contract's own way of saying "this tree is
    not in my image; put it wherever you like and name it in ``trees``",
    and it is reported only where that is true: no record at all, or a
    record entry naming no path.

    **A layer the record marks ``mounted`` is reported at the path the
    record names, not as ``null``.** The record means "not baked, mounted
    per session" by that flag, and reporting ``null`` for it read as "put
    it wherever you like" — which :meth:`_Build._workspace` then refuses,
    because west resolves project paths from ``.west/config`` plus the
    manifest and this program has no way to move them at invocation time.
    A backend that arranged itself by such a ``describe`` could never have
    built. ``describe`` is "**authoritative** about what the program can
    do" (§7.1), so it says the path the SDK has to be mounted at.

    §4 sanctions this since the D1 erratum: "A ``trees`` entry is the
    one thing a program may have a fixed path for", because a tree is a
    property of the *image* rather than of the session — "a declared
    path is then a requirement the backend MUST satisfy for that image,
    and not a convention". Declaring it here, in ``describe``, is the
    mechanism the erratum names: the backend learns the requirement
    before it starts a session, not from a refusal in the middle of one.

    ``version`` is the revision the record carries, and is omitted rather
    than guessed where there is none — §7.1.1 makes it optional for
    exactly that case, and a mounted tree is exactly that case.
    """
    layers = _record_layers(record)
    block: dict[str, dict[str, Any]] = {}
    for name in dict.fromkeys([*LAYERS, *sorted(layers)]):
        found = layers.get(name)
        entry: dict[str, Any] = found if isinstance(found, dict) else {}
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


class _Refused(Exception):
    """A typed answer an action already has; carries its result document.

    An action of this contract is a chain of checks that each end the
    invocation with a *document* rather than with a value, and ``build``
    (§7.2) is fourteen of them deep. Raising the finished document keeps
    the chain readable as the sequence §7.2 states it in, instead of as
    fourteen nested ``if`` statements — and every raise site is a
    ``reason`` from §5.4's registry, never a Python error class leaking
    out. It is caught in exactly one place per action.
    """

    def __init__(self, document: dict[str, Any]) -> None:
        super().__init__(document.get("reason"))
        self.document = document


def _open_context(echo: dict[str, Any], root: Path) -> ContextVerification:
    """The materialized context at *root*, measured, or a typed refusal.

    Shared by ``verify`` and ``build``, which is the point: both compute
    the effective context ID, and §3.3 is only worth anything while each
    side of the contract has *one* implementation of it. Every hash and
    the ID come from :func:`~mcuhome.workbench.contextdir.verify_context`.

    The refusals are §3.1's and §3.2's, and neither is action-specific:
    a context with no ``manifest.yaml`` "is missing a file the action
    needs" (§5.4), and a ``context`` format version this program does not
    implement is ``unsupported.context`` — "nothing about this context is
    broken" (§3.2). The manifest that cannot be read at all lands on
    ``error.context.mismatch``, which is a gap in contract v1 the module
    docstring records rather than papers over.

    What the caller does with :attr:`~ContextVerification.ok` differs, and
    that is why it is not decided here: ``verify`` exists to report a
    disagreement (§7.3), while for a ``build`` §5.4 makes ``result.context``
    a value "for comparison only" and never makes a mismatch a build
    failure.
    """
    # Lazy on purpose — see the note at the module's import block: the
    # SDK entry point imports this module under a bare runtime, and only
    # the actions that measure a context may pull the YAML machinery.
    from mcuhome.workbench.contextdir import ContextFormatVersionError, verify_context

    if not (root / MANIFEST_FILE).is_file():
        # "is missing a file the action needs … the missing path in
        # error.details" (§5.4). §3.1 makes this *the* file: "manifest.yaml
        # is the program's entry point; a program MUST NOT require any
        # out-of-band knowledge beyond it and this contract." A context
        # directory that is not there at all lands here too, which is
        # right — from the program's side the two are the same absence.
        raise _Refused(
            _failure(
                echo,
                _REASON_INCOMPLETE,
                f"the context at {root} carries no {MANIFEST_FILE}",
                details={"missing": [MANIFEST_FILE]},
            )
        )
    try:
        return verify_context(root)
    except ContextFormatVersionError as unimplemented:
        if unimplemented.found is None:
            raise _Refused(
                _failure(
                    echo,
                    _REASON_UNREADABLE,
                    f"the context at {root} states no {MANIFEST_FILE} format version",
                )
            ) from unimplemented
        raise _Refused(
            _refusal(
                echo,
                _REASON_CONTEXT,
                unimplemented.message,
                details={"context": _reportable(unimplemented.found)},
            )
        ) from unimplemented
    except (BuildError, OSError) as unreadable:
        # "found ``manifest.yaml`` and cannot read it as one: broken YAML,
        # a missing section, or a hash in a spelling §3.3.1 refuses" — the
        # reason §5.4's registry provides for exactly this case, distinct
        # from a mismatch so a backend can tell a corrupt manifest from a
        # tampered context file without parsing untrusted message text.
        detail = unreadable.message if isinstance(unreadable, BuildError) else str(unreadable)
        raise _Refused(
            _failure(
                echo,
                _REASON_UNREADABLE,
                f"the context at {root} cannot be read as one: {detail}",
            )
        ) from unreadable


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
    :func:`~mcuhome.workbench.contextdir.verify_context`, never from this module —
    see the module docstring for why a second implementation of §3.3 here
    would be the defect that rule exists against. Its
    :attr:`~mcuhome.workbench.contextdir.ContextVerification.ok` covers one case
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
    try:
        verification = _open_context(echo, root)
    except _Refused as refused:
        return refused.document

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
# build (§7.2)
# --------------------------------------------------------------------------


class _Events:
    """The optional NDJSON event stream of §8, or nothing at all.

    "Only if the request document carries ``events``. The program appends
    NDJSON to that file — one JSON object per line, UTF-8, flushed after
    every line, append-only, never truncated. Every object carries
    ``"event": "<name>"`` and a monotonic ``"seq"`` starting at 1."

    **Nothing here can fail an invocation.** "A program MUST NOT block on
    writing an event and MUST NOT die if the write fails. Where the two
    obligations collide — a full pipe, a stalled disk — **not blocking
    wins**." So every write is guarded, nothing is retried, and the file
    is flushed rather than ``fsync``ed: a reader tailing it wants the
    bytes now, and an event nobody read is not worth a build.
    """

    def __init__(self, path: Any) -> None:
        self._path = Path(path) if isinstance(path, str) else None
        self._seq = 0

    def emit(self, name: str, **fields: Any) -> None:
        if self._path is None:
            return
        self._seq += 1
        try:
            line = json.dumps({"event": name, "seq": self._seq, **fields})
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        except (OSError, TypeError, ValueError):
            return


def _write_file(path: Path, data: bytes) -> None:
    """Write *data* and make it real before anybody hashes it.

    The ``fsync`` is the point: §5.4 requires every declared hash to be
    read back from disk, and reading back a file whose bytes are still in
    the page cache would satisfy the letter and none of the reason — the
    backend re-hashes the same file from *its* side of the mount (§9.3).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, document: dict[str, Any]) -> None:
    """One of this program's own state files in ``work``, durably."""
    _write_file(path, (json.dumps(document, indent=2) + "\n").encode("utf-8"))


def patchset(layer_dir: Path) -> str:
    """``layers[<name>].patchset`` for one ``patches/<layer>/`` directory.

    §5.4 defines the value exactly, "otherwise a cross-implementation
    audit is worthless"::

        SHA-256( "mcuhome-patchset-1\\n"
                 + for each file under patches/<layer>/, ascending byte order:
                     <64 hex chars of the file's SHA-256> + " " + <filename> + "\\n" )

    "The value carries its own algorithm, so it is rendered ``sha256:`` +
    64 lowercase hex digits, and each ``<64 hex chars>`` inside the input
    is lowercase (§3.3.1)." The sort is over the filename's **bytes**, not
    over its code points — the two agree for the ``NNNN-name.patch``
    grammar :mod:`mcuhome.workbench.contextdir` enforces, and stating the byte order
    is what makes a second implementation agree for a name outside it.
    """
    files = sorted(
        (entry for entry in layer_dir.iterdir() if entry.is_file()),
        key=lambda entry: entry.name.encode("utf-8"),
    )
    text = _PATCHSET_PREFIX + "".join(f"{sha256_file(entry)} {entry.name}\n" for entry in files)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _absorb_tree(source: Path, target: Path) -> int:
    """Merge the generate child's *source* tree into *target*, content-aware.

    The other half of handing the child an empty ``out`` (§4): what it
    produced still has to end up in the session's persistent tree, and it
    must land there the way :func:`mcuhome.compiler.generate.write_tree` would
    have written it — a file whose bytes are already in *target* is left
    alone, mtime and all, because CMake watches the tree and a rewritten
    unchanged ``CMakeLists.txt`` re-runs the Matter sub-build. That is
    the whole difference between §7.2's ``incremental`` meaning
    something and meaning "clean, slowly".

    Nothing is deleted from *target*: the generator never deletes either
    (its contract is a mapping of files to write), and a stale file from
    an earlier model is exactly as stale after a direct
    ``write_tree(work/tree)`` would have run. Returns how many files the
    child produced, for the ``generate.written`` event.
    """
    produced = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        produced += 1
        destination = target / path.relative_to(source)
        content = path.read_bytes()
        if destination.is_file() and destination.read_bytes() == content:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return produced


def _run_child(command: list[str], *, env: dict[str, str], directory: Path) -> tuple[int, str]:
    """Run one short-lived child, and return its exit code and its output.

    The seam every test replaces. Standard error is merged into standard
    output because §8 makes the two "one raw, opaque log stream", and the
    stream is echoed onward as well as captured: a consumer "MUST NOT
    parse the log stream for machine decisions", and this program does not
    — it re-emits it so that the backend collecting the container's output
    sees what its children said, and keeps a copy only for the failure
    message.

    The west build does not come through here: it goes through
    :func:`~mcuhome.compiler.workspace.run_build`, which is the same shape with the
    live echoing a quarter of an hour of compiling needs.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            cwd=str(directory),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as failure:
        return 127, f"{command[0]}: {failure}"
    sys.stdout.write(completed.stdout)
    sys.stdout.flush()
    return completed.returncode, completed.stdout


def _requested_mode(document: dict[str, Any]) -> str:
    """``params.mode``, with §5.2's default applied.

    "An absent ``params``, a ``params`` object without a ``mode`` key, and
    ``params: {}`` are the same thing and all three mean ``mode:
    "clean"``". A value outside :data:`MODES` never reaches here — rule 3
    refused it (:data:`_NEEDED_FIELDS`) — so the fallback below is the
    default and not a guess.
    """
    params = document.get("params")
    if not isinstance(params, dict):
        return DEFAULT_MODE
    value = params.get("mode", DEFAULT_MODE)
    return value if value in MODES else DEFAULT_MODE


def _is_sdk_version(value: Any) -> bool:
    """An ``mcuhome-sdk.json`` format version this program implements."""
    return isinstance(value, int) and not isinstance(value, bool) and value in SDK_METADATA_VERSIONS


def sdk_entry_point(sdk_path: Path) -> tuple[Path, str]:
    """The code-generation entry point declared at the root of ``trees.sdk``.

    §6.1, normative: ``mcuhome-sdk.json``, "one JSON object, UTF-8 without
    BOM, RFC 8259, read with the JSON parser §5.1 already requires and
    nothing more", fixing three names and no values — ``sdk``,
    ``generate.program``, ``generate.runtime``. Returns the absolute path
    of the program and the runtime string, and raises
    :class:`~mcuhome.model.errors.BuildError` for everything §6.1 calls "code
    generation cannot be reached".

    "A missing file, a missing field and a ``sdk`` version the program
    does not implement are all one situation … and all three fail the
    invocation with ``reason: "error.build.failed"``. They are not
    ``unsupported``: the program implements everything this contract asks
    of it, and no other container would fare better with this SDK
    package." :meth:`_Build._sdk_metadata` is where that becomes a result
    document; here it is an error a caller can also raise while checking
    an SDK package it is *shipping*, which is the second reader this
    function has.

    ``generate.runtime`` is read and not interpreted: it is "an opaque
    string", and the honest consequence the contract states is that a
    conforming container must *provide* the runtime, not that it can check
    the name against anything. It is required to be there because the
    contract fixes the field.
    """
    path = sdk_path / SDK_METADATA_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        raise BuildError(
            f"code generation cannot be reached: {path} — {failure}",
            hint=f"an SDK package declares its entry point in {SDK_METADATA_FILE} (§6.1)",
        ) from failure
    generate = data.get("generate") if isinstance(data, dict) else None
    version = data.get("sdk") if isinstance(data, dict) else None
    program = generate.get("program") if isinstance(generate, dict) else None
    runtime = generate.get("runtime") if isinstance(generate, dict) else None
    hint = "§6.1 fixes the file name and the three field names, and no values"
    if not _is_sdk_version(version):
        raise BuildError(
            f"{path} states sdk metadata version {version!r}; this program "
            f"implements {list(SDK_METADATA_VERSIONS)}",
            hint=hint,
        )
    if not isinstance(program, str) or not isinstance(runtime, str):
        raise BuildError(
            f"{path} does not declare both generate.program and generate.runtime",
            hint=hint,
        )
    relative = Path(program)
    if relative.is_absolute() or ".." in relative.parts:
        raise BuildError(
            f"{path} declares generate.program as {program!r}, and §6.1 makes it "
            "a path relative to the root of trees.sdk",
            hint=hint,
        )
    return sdk_path / relative, runtime


class _Build:
    """One ``build`` invocation (§7.2), in the order the contract states it.

    The steps, each a method below and each ending either in the next one
    or in a ``reason`` from §5.4's registry:

    1. measure the context and its effective ID (§3.3, shared with
       ``verify``);
    2. refuse a context without ``keys/signing.pub`` (§7.2);
    3. claim ``work`` against a foreign session marker (§6.3);
    4. resolve the west workspace against the ``trees`` the backend named
       (§6.1);
    5. build the child environment (§6.1, §10);
    6. apply the patches of every patched layer, once per session (§6.2);
    7. reach code generation through the SDK entry point (§6.1);
    8. compile with ``west build --sysbuild`` (stage 5, unchanged);
    9. deliver the artifacts into ``out`` and write the build report
       (§7.2, §7.2.1).

    Nothing here writes into the context: "Build outputs MUST NOT be
    written into the context. The context is a read-only input for the
    whole life of a session" (§3.1). The write scope is ``out``, ``work``,
    ``tmp``, the writable trees and the three files the request names
    (§9.2 point 1), and every path comes out of the request document.
    """

    def __init__(
        self,
        echo: dict[str, Any],
        document: dict[str, Any],
        record: Path,
        env: dict[str, str] | None,
    ) -> None:
        self.echo = echo
        self.document = document
        self.record = record
        #: The environment the program was *told* it runs in, never read
        #: out of the process — see the module docstring. Empty means the
        #: children get only what this program derives.
        self.base_env = dict(env or {})
        self.events = _Events(document.get("events"))
        #: The effective context ID, once measured. Until then no result
        #: document may carry one (§5.4).
        self.measured: str | None = None
        self.context_root = Path(document["context"])
        self.out_dir = Path(document["out"])
        self.work_dir = Path(document["work"])
        self.tmp_dir = Path(document["tmp"])
        self.session = document["session"]
        self.jobs = int(document["limits"]["jobs"])
        self.mode = _requested_mode(document)
        given = document.get("trees")
        self.given_trees: dict[str, Any] = given if isinstance(given, dict) else {}

    # -- refusals ----------------------------------------------------------

    def fail(self, reason: str, message: str, details: dict[str, Any] | None = None) -> _Refused:
        """A ``failure`` result carrying whatever this invocation measured.

        §5.4: "An invocation that failed before it got that far reports
        what it measured and nothing more" — so ``context`` is present
        from the moment :attr:`measured` is set and absent before it, and
        ``artifacts`` and ``layers`` never appear on a failure at all,
        because both are rows qualified "on success".
        """
        return _Refused(
            _failure(self.echo, reason, message, details=details, context=self.measured)
        )

    # -- the chain ---------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """The whole invocation, and the one place a refusal is caught."""
        self.events.emit("invocation.started", action="build")
        try:
            result = self._body()
        except _Refused as refused:
            result = refused.document
        self.events.emit("invocation.finished", status=result["status"])
        return result

    def _body(self) -> dict[str, Any]:
        verification = _open_context(self.echo, self.context_root)
        self.measured = verification.actual_id
        self.events.emit("context.checked", context=self.measured)
        self._require_signing_key()

        warm = self._claim_work()
        # §7.2: "An `incremental` for which the program finds no prior
        # state of *this session* in `work` is executed as `clean`." The
        # marker is the predicate — see the module docstring for why this
        # program writes one at all.
        mode = self.mode if warm else DEFAULT_MODE

        topdir, layer_paths = self._workspace()
        env = self._environment(topdir, layer_paths["sdk"])
        layers = self._apply_patches(layer_paths, env)

        if mode == DEFAULT_MODE:
            self._discard_state()
        tree = self._generate(layer_paths["sdk"], env)
        scheme, build_dir, log = self._compile(topdir, tree, env, mode)
        artifacts = self._collect(scheme, build_dir, log)
        return _result_document(
            self.echo,
            _STATUS_SUCCESS,
            context=self.measured,
            artifacts=artifacts,
            layers=layers,
        )

    # -- 2. the verification key (§7.2) ------------------------------------

    def _require_signing_key(self) -> None:
        """No ``keys/signing.pub``, no build — and no fallback.

        "A context submitted to a ``build`` that does not carry it fails
        the invocation typed — ``status: "failure"``, ``reason:
        "error.context.incomplete"``, the missing path in
        ``error.details`` — and the program MUST NOT build anyway. There
        is no fallback to MCUboot's default key, because that default is
        MCUboot's demo key and **its private half is published**."

        A key that is present but unusable — wrong curve, a private key by
        mistake — is not typed by contract v1 at all (§7.2 types only the
        absence), so nothing is checked here beyond existence: the file is
        handed to sysbuild, and MCUboot's own tooling is the thing that
        knows what a verification key is.
        """
        if not (self.context_root / SIGNING_KEY_FILE).is_file():
            raise self.fail(
                _REASON_INCOMPLETE,
                f"a build needs {SIGNING_KEY_FILE} as the bootloader's verification key, "
                f"and the context at {self.context_root} carries none",
                {"missing": [SIGNING_KEY_FILE]},
            )

    # -- 3. work (§6.3) ----------------------------------------------------

    def _claim_work(self) -> bool:
        """Read the session marker, then own it. Returns "warm".

        §6.3: "A program that records a marker **MUST read it before using
        anything in ``work``**, on every invocation. A guard that is
        written and not read is worse than no guard, because it looks like
        one." So this is the first thing that touches ``work``.

        A marker naming another session "is terminal for the invocation …
        it MUST NOT use the state it found, MUST NOT delete or overwrite
        it, and MUST NOT fall back to a private working area of its own
        choosing. It writes nothing into ``work`` in this case, not even
        its own marker." A marker that cannot be parsed is treated the
        same way, for the reason the module docstring gives.

        The return value answers §7.2's ``incremental`` predicate, and
        only that: "no prior state of this session" is a marker this
        invocation had to write, and a marker it found is a previous
        invocation of the same session.
        """
        marker = self.work_dir / WORK_MARKER
        if marker.exists():
            try:
                found = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                found = None
            recorded = found.get("session") if isinstance(found, dict) else None
            if not isinstance(found, dict) or recorded != self.session:
                raise self.fail(
                    _REASON_WORK,
                    f"the work directory {self.work_dir} is marked for another session",
                    {"session": self.session, "found": recorded},
                )
            return True
        _write_json(marker, {"session": self.session})
        return False

    def _discard_state(self) -> None:
        """``clean`` — "fresh workspace" (§7.2), concretely.

        The generated application tree and the CMake tree are this
        program's own state in ``work`` (module docstring), so a fresh
        workspace is those two removed. The session marker and the
        per-layer patch records stay: §6.2 makes patch application "once
        per session" and explicitly not once per invocation, and a
        ``clean`` that re-applied patches would either fail on the second
        build of a session or need a layer reset — "There is no layer
        reset in this contract".
        """
        for name in (WORK_TREE, WORK_BUILD):
            shutil.rmtree(self.work_dir / name, ignore_errors=True)

    # -- 4. the workspace (§6.1) -------------------------------------------

    def _workspace(self) -> tuple[Path, dict[str, Path]]:
        """The west workspace this program carries, checked against ``trees``.

        §6.1 assigns the program the whole build environment and names no
        mechanism for pointing an existing west workspace at a path the
        backend chose at invocation time. This is where that ends: the
        image's own record (``containers/builder/workspace-record.py``)
        says where each layer is, and a ``trees`` entry is accepted only
        where it names that same path — which is exactly what a backend
        mounting a writable view *over* the baked tree produces, and what
        ``containers/builder/Dockerfile`` already anticipates. Anything
        else is ``error.build.failed`` naming the layer and both paths.
        See the module docstring for what would replace this.

        A program with no record has no workspace at all, and says so
        rather than compiling against nothing. That is consistent with the
        ``describe`` it would have given: every layer ``"path": null``,
        which asks the backend to supply trees this program then could not
        use — and with :func:`honoured_required`, which honours no
        ``/trees/<layer>`` value at all in that case.
        """
        document = _record_document(self.record)
        topdir = document.get("topdir")
        recorded = document.get("layers")
        if not isinstance(topdir, str) or not isinstance(recorded, dict):
            raise self.fail(
                _REASON_BUILD,
                f"this program carries no west workspace: {self.record} names none, and "
                "§6.1 makes assembling one the program's responsibility",
            )
        paths = _record_tree_paths(self.record)
        missing = [name for name in LAYERS if name not in paths]
        if missing:
            raise self.fail(
                _REASON_BUILD,
                f"the west workspace at {topdir} has no {', '.join(missing)} layer",
            )
        for name, entry in self.given_trees.items():
            if name not in paths or not isinstance(entry, dict):
                continue
            given = entry.get("path")
            if isinstance(given, str) and Path(given) != paths[name]:
                raise self.fail(
                    _REASON_BUILD,
                    f"this program builds the {name} layer at {paths[name]} and cannot "
                    f"move its west workspace to {given}; mount the view there instead",
                )
        return Path(topdir), paths

    # -- 5. the child environment (§6.1, §10) ------------------------------

    def _environment(self, topdir: Path, sdk_path: Path) -> dict[str, str]:
        """What every child of this invocation runs in.

        Assembled from what this program was *told* it runs in — never
        read out of the process (the module docstring) — by
        :func:`~mcuhome.compiler.workspace.build_environment`, which is the one
        definition of a Matter build environment in this package and is
        also what :func:`~mcuhome.compiler.container.container_environment` reaches
        for the other direction. It contributes the codegen shim on
        ``PYTHONPATH``, the two job caps that nothing inherits,
        ``ZEPHYR_BASE`` so the generated CMakeLists finds Zephyr and the
        Matter SDK next to it, a writable ``HOME``, and the ``TMPDIR`` §4
        makes the program point at the request's ``tmp``.

        **``HOME`` is in ``work``, and it is not decoration.** The backend
        "runs the program as the calling user where it can" (§2.2), which
        in a container is a UID with no ``/etc/passwd`` entry; tools that
        cache in ``$HOME`` fail obscurely without a writable one
        (:data:`mcuhome.compiler.container.CONTAINER_HOME` records what that cost).
        ``work`` rather than ``tmp`` because those caches are worth
        keeping for the next invocation of the session, and §9.2 point 1
        makes ``work`` a place this program may write.

        **What is *not* built here is ``PATH``.** It arrives with the
        environment the caller stated, because it is the one variable
        naming things this program did not put anywhere: the image's
        toolchain, west, ``git``, the runtime ``generate.runtime`` names.
        A program that composed one would be describing a filesystem the
        contract does not own (§4). :func:`~mcuhome.compiler.workspace.require_tools`
        is called on the finished environment before anything is compiled,
        so an environment that cannot start ``west`` is a typed refusal
        naming the tool rather than a child process that fails to exec ten
        minutes in.

        ``limits.jobs`` is taken as given. It is "**authoritative** and
        mandatory for working actions … An optional field would be
        worthless here: a foreign program would fall back to ``nproc``,
        which is exactly the case the field exists against" — so
        :func:`~mcuhome.compiler.workspace.resolve_jobs` and its auto-detection stay
        on the host side of the contract and are never called here.

        The cache follows §10 exactly. A ``writable: true`` shared cache
        "MAY be used as the primary cache"; a ``writable: false`` one
        "MUST be treated as a read-only secondary cache, with its own
        primary cache in ``work`` or ``tmp``", which is what the two
        variables below say to ccache; and an absent one leaves the
        program with a cache of its own that dies with the session.
        ``writable`` is read and never probed (§4.1).
        """
        home = self.work_dir / WORK_HOME
        home.mkdir(parents=True, exist_ok=True)
        # Zephyr's user cache (the toolchain capability database) goes
        # where XDG_CACHE_HOME points, then $HOME/.cache — but each
        # candidate only counts if it already EXISTS and is writable
        # (scripts/build/dir_is_writeable.py is a bare os.access), and a
        # fresh HOME has no .cache yet. The fallback is ZEPHYR_BASE/.cache
        # — inside the frozen workspace, where the first configure then
        # dies on the same wall as every other workspace write. So the
        # cache home is stated explicitly and created before anything
        # asks.
        cache_home = self.work_dir / WORK_XDG_CACHE
        cache_home.mkdir(parents=True, exist_ok=True)
        # West caches what it derives: the first `west build` writes
        # `zephyr.base` back into the workspace's own `.west/config`. The
        # baked workspace belongs to whoever built the image, and §2.2
        # runs this program as the calling user — but the deeper point is
        # that the workspace is a frozen input this program never writes,
        # incidental caches included. So the local config is copied into
        # `work` once per session and ``WEST_CONFIG_LOCAL`` points west at
        # the copy: topdir discovery still walks to `.west/`, only the
        # file west reads and writes moves, and any west write invented
        # later lands in `work` with it.
        west_config = self.work_dir / WORK_WEST_CONFIG
        if not west_config.exists():
            try:
                shutil.copyfile(topdir / ".west" / "config", west_config)
            except OSError as error:
                raise self.fail(
                    _REASON_BUILD,
                    f"the west workspace at {topdir} has no readable "
                    f".west/config: {error.strerror}",
                ) from error
        env = workspace.build_environment(
            self.base_env,
            jobs=self.jobs,
            pyshim_dir=sdk_path / workspace.PYSHIM_SUBDIR,
            zephyr_base=topdir / "zephyr",
            tmpdir=self.tmp_dir,
            home=home,
        )
        env["WEST_CONFIG_LOCAL"] = str(west_config)
        env["XDG_CACHE_HOME"] = str(cache_home)
        cache = self.document.get("ccache")
        shared = cache.get("path") if isinstance(cache, dict) else None
        if isinstance(cache, dict) and cache.get("writable") is True:
            env["CCACHE_DIR"] = str(shared)
        else:
            env["CCACHE_DIR"] = str(self.work_dir / WORK_CCACHE)
            if isinstance(shared, str):
                # ccache >= 4.8 spells the secondary store this way; the
                # `|read-only` attribute is what makes the MUST above true
                # rather than merely intended.
                env["CCACHE_REMOTE_STORAGE"] = f"file:{shared}|read-only"
        env["CCACHE_BASEDIR"] = str(topdir)
        try:
            workspace.require_tools(env)
        except BuildError as unusable:
            raise self.fail(
                _REASON_BUILD,
                f"this invocation cannot run a build in the environment it was given: "
                f"{unusable.message}",
            ) from unusable
        return env

    # -- 6. patched layers (§6.2) ------------------------------------------

    def _apply_patches(self, paths: dict[str, Path], env: dict[str, str]) -> dict[str, Any]:
        """Apply every patched layer's patches, once per session.

        §6.2 fixes the semantics and leaves the tool free: "``git apply``,
        ``patch -p1``, or a diff implementation the program brings itself
        are all conforming". This uses ``git apply`` with no ``--3way`` and
        no fallback, which is what ``patches/README.md``, CI and the image
        build already do — and it is what "a patch that does not apply is
        a failure of the invocation and not something to search around"
        asks for.

        The per-layer record in ``work`` is what "once per session" means:
        started is written before the first patch, complete only "after
        the last patch of that layer applied cleanly", and a layer found
        started-but-not-complete is ``error.patch.incomplete`` — terminal,
        because "restoring the pristine baseline is not possible from
        inside the merged view at all".

        Every patched layer appears in the returned ``layers`` block,
        including one an earlier invocation of this session already
        applied: §5.4 makes the block mandatory "for every patched layer"
        of a successful build, and the patch set of a locked context
        cannot change.
        """
        root = self.context_root / PATCHES_DIR
        names = (
            sorted(entry.name for entry in root.iterdir() if entry.is_dir())
            if root.is_dir()
            else []
        )
        records = self.work_dir / WORK_PATCH_RECORDS
        layers: dict[str, Any] = {}
        for name in names:
            # §6.2 types both of its conditions the same way: "If
            # `patches/<layer>/` names a layer for which there is no
            # `trees` entry, **or** which the program does not know, the
            # program MUST NOT proceed: `status: "failure"`, `reason:
            # "error.layer.unknown"`."
            if name not in LAYERS or name not in paths:
                raise self.fail(
                    _REASON_LAYER,
                    f"the context patches a layer this program has no tree for: {name}",
                    {"layer": name},
                )
            entry = self.given_trees.get(name)
            if not isinstance(entry, dict):
                raise self.fail(
                    _REASON_LAYER,
                    f"the context patches the {name} layer and the request names no "
                    f"trees entry for it (§4.1: the backend MUST supply one)",
                    {"layer": name},
                )
            if entry.get("writable") is not True:
                # The third case, and §6.2 does not type it: the entry is
                # there and does not assert what §4.1 makes the backend
                # assert for a patched layer. It is not "no entry" and not
                # "a layer this program does not know", so it is not
                # `error.layer.unknown`; it is the ordinary one.
                raise self.fail(
                    _REASON_BUILD,
                    f"the {name} layer carries patches and the request names no writable "
                    "view of it (§4.1, §6.2)",
                    {"layer": name},
                )
            digest = patchset(root / name)
            layers[name] = {"patchset": digest}
            state = records / f"{name}.json"
            if state.exists():
                try:
                    recorded = json.loads(state.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    recorded = {}
                if isinstance(recorded, dict) and recorded.get("state") == "complete":
                    continue
                raise self.fail(
                    _REASON_PATCH,
                    f"the {name} layer was recorded as started and never completed; "
                    "this session cannot be recovered, start a new one",
                    {"layer": name},
                )
            _write_json(state, {"layer": name, "patchset": digest, "state": "started"})
            patches = sorted((root / name).iterdir(), key=lambda path: path.name.encode("utf-8"))
            for patch in patches:
                command = ["git", "-C", str(paths[name]), "apply", "--verbose", "-p1", str(patch)]
                code, log = _run_child(command, env=env, directory=paths[name])
                if code != 0:
                    raise self.fail(
                        _REASON_BUILD,
                        f"{patch.name} does not apply to the {name} layer: {log.strip()}",
                        {"layer": name},
                    )
            _write_json(state, {"layer": name, "patchset": digest, "state": "complete"})
            self.events.emit("patch.layer.applied", layer=name, count=len(patches))
        return layers

    # -- 7. code generation (§6.1) -----------------------------------------

    def _sdk_metadata(self, sdk_path: Path) -> tuple[Path, str]:
        """:func:`sdk_entry_point`, as a refusal of *this* invocation.

        The reading of ``mcuhome-sdk.json`` is a module-level function so
        that the SDK package's own suite can hold its metadata against the
        rules the program applies to it, rather than against a second
        transcription of §6.1 in a fixture.
        """
        try:
            return sdk_entry_point(sdk_path)
        except BuildError as unreachable:
            raise self.fail(_REASON_BUILD, unreachable.message) from unreachable

    def _generate(self, sdk_path: Path, env: dict[str, str]) -> Path:
        """Invoke the SDK entry point as a child, over this same ABI.

        §6.1: ``<trees.sdk.path>/<generate.program> generate <absolute path
        of a request document>`` — "That is §5.1 unchanged … The program
        writes that request document into its own ``tmp`` and is the
        *backend* of that invocation, in exactly the sense §1.1 defines."

        Two things about the invocation are fixed and both are here: "the
        entry point reads the build context from ``context`` and writes
        the per-device Zephyr application tree into ``out``". Where that
        ``out`` is, is this program's choice as backend — and as backend
        it owes the child what §4's table promises every invocation: an
        ``out`` that is **empty**. So the child writes into a fresh
        per-invocation directory under ``tmp``, and what it produced is
        then absorbed into the session's ``work/tree`` content-aware —
        a file whose bytes are already there is left alone, mtime and
        all. Handing the child ``work/tree`` directly would be cheaper
        and wrong twice over: a foreign SDK entry point (E30's whole
        point is that it need not be MCUHome's) may rely on the emptiness
        the contract states, and one that lists ``out`` before writing
        would see another invocation's files. The absorb is what keeps
        §7.2's ``incremental`` meaningful — CMake watches the tree's
        mtimes, so an unchanged file must stay untouched.
        "Everything else in the document is between the SDK package and
        itself"; the rest of §5.2's working-action fields are sent because
        the entry point speaks this ABI and they are mandatory in it.

        "A non-zero exit, a missing result document or a ``status`` other
        than ``success`` fails the invocation with ``reason:
        "error.build.failed"``."
        """
        entry, _runtime = self._sdk_metadata(sdk_path)
        tree = self.work_dir / WORK_TREE
        scratch = self.tmp_dir / GENERATE_ACTION
        scratch.mkdir(parents=True, exist_ok=True)
        tree.mkdir(parents=True, exist_ok=True)
        child_out = scratch / "out"
        child_out.mkdir(parents=True, exist_ok=True)
        child_work = self.work_dir / GENERATE_ACTION
        child_work.mkdir(parents=True, exist_ok=True)
        request_path = scratch / "request.json"
        result_path = scratch / "result.json"
        _write_json(
            request_path,
            {
                "request": REQUEST_VERSIONS[0],
                "result": str(result_path),
                "session": self.session,
                "out": str(child_out),
                "work": str(child_work),
                "tmp": str(scratch),
                "context": str(self.context_root),
                "trees": {"sdk": dict(self.given_trees["sdk"])},
                "limits": {"jobs": self.jobs},
            },
        )
        code, log = _run_child(
            [str(entry), GENERATE_ACTION, str(request_path)], env=env, directory=scratch
        )
        if code != 0:
            raise self.fail(
                _REASON_BUILD,
                f"code generation exited {code}: {log.strip()}",
            )
        try:
            answer = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as failure:
            raise self.fail(
                _REASON_BUILD,
                f"code generation wrote no readable result document: {failure}",
            ) from failure
        if not isinstance(answer, dict) or answer.get("status") != _STATUS_SUCCESS:
            said = answer.get("reason") if isinstance(answer, dict) else answer
            raise self.fail(_REASON_BUILD, f"code generation did not succeed: {said!r}")
        written = _absorb_tree(child_out, tree)
        self.events.emit("generate.written", files=written)
        return tree

    # -- 8. the compile (stage 5) ------------------------------------------

    def _compile(
        self, topdir: Path, tree: Path, env: dict[str, str], mode: str
    ) -> tuple[Any, Path, str]:
        """``west build --sysbuild``, assembled by stage 5 and run by it.

        Nothing about the command is decided here:
        :func:`~mcuhome.compiler.workspace.west_build_command` already knows the
        per-image snippet rule, and its ``detached_signing`` path is
        exactly what §7.2 requires — "It MUST use ``keys/signing.pub``
        from the context as the bootloader's verification key" and "The
        program MUST NOT sign images". With that flag the key handed to
        sysbuild is the public half and the generated tree clears the
        application's signing step, so no signed file is produced at all.

        The device model comes out of the context (§3.1 puts it at
        ``model/device-model.json``) through
        :func:`~mcuhome.model.modelfile.read_model`, which is documented as
        exactly this receiving end. A board this builder has no update scheme for
        is ``error.build.failed``: §7.2.1 makes the ``signing`` block
        mandatory, and there is nothing to put in it.

        ``clean`` is ``--pristine always`` regardless of what the build
        directory looks like; ``incremental`` lets
        :func:`~mcuhome.compiler.workspace.pristine_mode` answer, which is the
        function that already knows the one case ``auto`` cannot cover.
        """
        try:
            model = read_model(self.context_root / MODEL_FILE)
        except BuildError as failure:
            raise self.fail(_REASON_BUILD, failure.message) from failure
        board = registry.BOARDS.get(model.device.board)
        scheme = None if board is None else board.update_scheme
        if scheme is None:
            raise self.fail(
                _REASON_BUILD,
                f"this builder has no MCUboot layout for {model.device.board}, so it "
                "cannot state the signing parameters §7.2.1 makes mandatory",
            )
        app_dir = tree / APP_DIR
        build_dir = self.work_dir / WORK_BUILD
        plan = workspace.BuildPlan(
            topdir=topdir,
            app_dir=app_dir,
            build_dir=build_dir,
            command=workspace.west_build_command(
                app_dir=app_dir,
                build_dir=build_dir,
                board=model.device.board,
                snippets=tuple(model.build.snippets),
                bootloader_snippets=scheme.bootloader_snippets,
                signing_key=self.context_root / SIGNING_KEY_FILE,
                detached_signing=True,
                jobs=self.jobs,
                pristine="always" if mode == DEFAULT_MODE else workspace.pristine_mode(build_dir),
            ),
            env=env,
        )
        try:
            code, log = workspace.run_build(plan)
        except BuildError as failure:
            # The hint carries what the message cannot — for a build that
            # never started, the exact command line — and §5.4's details
            # is the field that exists for it. Dropping it once reduced
            # "could not start the build: No such file or directory" to a
            # riddle with no file name in it.
            details = {"hint": failure.hint} if failure.hint else None
            raise self.fail(_REASON_BUILD, failure.message, details) from failure
        if code != 0:
            raise self.fail(_REASON_BUILD, f"west build exited with {code}")
        return scheme, build_dir, log

    # -- 9. what leaves (§7.2, §7.2.1) -------------------------------------

    def _deliver(self, source: Path, name: str, role: str) -> dict[str, Any]:
        """Copy one file into ``out`` and declare it.

        The four mandatory fields of §5.4 and nothing else: ``root`` is
        ``"out"``, the only legal value in v1; ``path`` is relative to it
        with segments matching ``[A-Za-z0-9._-]+``; ``role`` identifies it
        by function; ``hashes`` is keyed by algorithm and read back from
        disk. No size — "an artifact entry declares no size".

        Copied rather than declared where the linker left it, because §7.2
        states what MCUHome's own container writes and because everything
        in ``out`` is then something this program put there deliberately:
        sysbuild's combined hex, which on a never-signed build is the
        *unsigned* application under a flashable-looking name, simply
        never arrives.
        """
        destination = self.out_dir / name
        _write_file(destination, source.read_bytes())
        digest = sha256_file(destination)
        self.events.emit(
            "artifact.collected", role=role, path=name, size=destination.stat().st_size
        )
        return {"root": "out", "path": name, "role": role, "hashes": {"sha256": digest}}

    def _collect(self, scheme: Any, build_dir: Path, log: str) -> list[dict[str, Any]]:
        """The artifacts a successful build declares (§7.2).

        "A successful device build MUST declare at least two artifacts:
        the unsigned image with role ``firmware`` … and **exactly one
        artifact with role ``report``**". Both firmware files carry the
        ``firmware`` role and the bootloader is declared as well — the
        module docstring says why for each.

        There is no ``ota`` role and no ``.ota`` file: "The OTA wrapper's
        payload has to be the **signed** binary and the same contract
        forbids the program to sign, so the requirement cancelled itself."
        """
        images = workspace.build_images(build_dir, app_image=APP_DIR)
        app_output = build_dir / APP_DIR / "zephyr"
        artifacts: list[dict[str, Any]] = []
        for produced, delivered in FIRMWARE_ARTIFACTS:
            source = app_output / produced
            if not source.is_file():
                raise self.fail(
                    _REASON_BUILD,
                    f"the build produced no {produced} for the application image",
                )
            artifacts.append(self._deliver(source, delivered, "firmware"))
        produced, delivered = BOOTLOADER_ARTIFACT
        bootloader = build_dir / workspace.BOOTLOADER_IMAGE / "zephyr" / produced
        if bootloader.is_file():
            artifacts.append(self._deliver(bootloader, delivered, "bootloader"))

        memory = workspace.parse_image_memory_report(log, images=[image.name for image in images])
        regions = [
            {
                "image": image,
                "region": region.name,
                "used": region.used,
                "total": region.total,
                "percent": region.percent,
            }
            for image, found in memory.items()
            for region in found
        ]
        for region in regions:
            self.events.emit("build.memory.region", **region)
        _write_file(
            self.out_dir / REPORT_ARTIFACT,
            (json.dumps(self._report(scheme, build_dir, regions), indent=2) + "\n").encode("utf-8"),
        )
        artifacts.append(
            {
                "root": "out",
                "path": REPORT_ARTIFACT,
                "role": "report",
                "hashes": {"sha256": sha256_file(self.out_dir / REPORT_ARTIFACT)},
            }
        )
        self.events.emit(
            "artifact.collected",
            role="report",
            path=REPORT_ARTIFACT,
            size=(self.out_dir / REPORT_ARTIFACT).stat().st_size,
        )
        return artifacts

    def _report(
        self, scheme: Any, build_dir: Path, regions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The build report of §7.2.1 — one JSON object, for one consumer.

        "It exists for one consumer and one purpose: the client that signs
        detached, which is the only party holding the private key." So it
        carries the ``report`` version, the mandatory ``signing`` block and
        the optional ``memory`` list, and none of what
        ``build-manifest.json`` carries: the contract strikes ``signed``,
        ``signed_by_the_build``, ``inputs`` and ``outputs`` by name,
        because "a build container never signs, the input is the
        ``firmware`` artifact, and where the signed output goes is the
        signer's business".

        The four arguments are :func:`~mcuhome.compiler.report.signing_parameters`
        unchanged — three of them board data the build already had to know
        and the fourth, imgtool's ``--version``, read out of the built
        application's own ``.config``, which is the behaviour §7.2.1 cites.
        A build that left no ``.config`` behind has no version to state,
        and stating Zephyr's ``0.0.0+0`` default for it would produce a
        signed image MCUboot compares monotonically against the wrong
        number — so that is ``error.build.failed`` rather than a report.

        ``memory`` is omitted when the build relinked nothing: "A build
        that relinked nothing reports none, which is correct rather than
        incomplete."
        """
        kconfig = build_dir / APP_DIR / "zephyr" / ".config"
        if not kconfig.is_file():
            raise self.fail(
                _REASON_BUILD,
                f"the build left no {kconfig.name} for the application image, so the "
                "signing parameters §7.2.1 makes mandatory cannot be stated",
            )
        try:
            parameters = report.signing_parameters(scheme, kconfig=report.read_kconfig(kconfig))
        except BuildError as failure:
            raise self.fail(_REASON_BUILD, failure.message) from failure
        document: dict[str, Any] = {
            "report": REPORT_VERSION,
            "signing": {
                "signature_type": registry.SIGNATURE_TYPE,
                "arguments": parameters.to_dict(),
            },
        }
        if regions:
            document["memory"] = regions
        return document


def _build(
    echo: dict[str, Any], document: dict[str, Any], record: Path, env: dict[str, str] | None
) -> dict[str, Any]:
    """``build`` (§7.2): the one entry point, so the class stays private."""
    return _Build(echo, document, record, env).run()


# --------------------------------------------------------------------------
# The invocation (§5.1)
# --------------------------------------------------------------------------


def _invoke(
    action: str, document: dict[str, Any], record: Path, env: dict[str, str] | None
) -> dict[str, Any]:
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

    offending = _unhonourable(action, document, record)
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

    bad_mode = _unsupported_mode(action, document)
    if bad_mode is not None:
        return refuse(
            _REASON_REQUIRED,
            f"this program implements the build modes {list(MODES)}, not {bad_mode!r}",
            details={"required": ["/params/mode"]},
        )

    if action == "verify":
        return _verify(echo, document)
    if action == "build":
        return _build(echo, document, record, env)
    return _describe(echo, record)


def main(
    argv: list[str],
    *,
    record: Path = WORKSPACE_RECORD,
    env: dict[str, str] | None = None,
) -> int:
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

    *env* is the environment a ``build``'s child processes start from, and
    it is **stated by whoever started this program, never read out of the
    process**. That distinction is the invariant
    ``tests_py/test_userpaths.py`` enforces on every module here: one
    process serves several sessions, and a call-time ``os.environ`` is
    what makes two of them answer each other's questions. A caller that
    states nothing gets children that run in exactly what §6.1 makes the
    program's own responsibility and nothing else — which is correct and
    thin, and is why ``containers/builder/run`` will have to hand its
    image's environment over before the first real compile happens inside
    the image (the other half of that, ``mcuhome-sdk.json``, is §6.1's and
    lives in the SDK package).

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
    return run_invocation(argv, lambda action, document: _invoke(action, document, record, env))


def run_invocation(
    argv: list[str],
    invoke: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> int:
    """§5.1's outer sequence around *invoke*; the return value is the exit code.

    Shared between the program itself (:func:`main`) and the SDK
    package's entry point (:mod:`mcuhome.compiler.sdkentry`), because §6.1 reuses
    the invocation ABI on purpose: "A second calling convention would be
    a second frozen thing … this way the entry point is reached with the
    parser, the two documents and the four exit values every conforming
    program has anyway." Sharing the sequence is what keeps that a fact
    of the code rather than a claim about two transcriptions of it.

    **A crash inside *invoke* becomes a result document, not a
    traceback.** §5.3's exit 1 "promises a result document", and the
    table leaves every other exit as "the program died. Undefined
    forever" — so an unexpected exception after the preamble was read is
    answered on the channel that was already open: ``status:
    "failure"``, the exception in ``error.message``, exit 1. The
    ``reason`` is ``error.internal`` — the registry value §5.4.1's
    erratum added for exactly this ("the program itself failed, in any
    action"), so a backend is never told a ``describe`` or ``verify``
    crash was a build-work failure. Only when even that document cannot
    be written does the invocation end with exit 66 — the same answer
    the ordinary write path gives, because a result nobody can address
    is that case whatever was computed.
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

    echo: dict[str, Any] = {"action": action}
    if "session" in document:
        echo["session"] = document["session"]
    try:
        result = invoke(action, document)
    except Exception as died:  # noqa: BLE001 - the catch-all IS the contract duty
        result = _result_document(
            echo,
            _STATUS_FAILURE,
            reason=_REASON_INTERNAL,
            error={
                "retryable": False,
                "message": f"the program failed inside the action: {died}",
                "details": {},
            },
        )

    try:
        _write_atomically(destination, result)
    except OSError:
        return EXIT_UNUSABLE

    return EXIT_SUCCESS if result["status"] == _STATUS_SUCCESS else EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover - the launcher's entry point
    import os
    import sys

    # THE process boundary, and the one place that may read process
    # state: when this module is the process, it is "whoever started
    # this program", and the environment it hands over is the image's
    # own — PATH with the toolchain and west, exactly what
    # :func:`main`'s docstring says the launcher owes its children.
    # ``tests_py/test_userpaths.py`` exempts this guard by shape and
    # pins the handover itself; library imports never execute it.
    raise SystemExit(main(sys.argv, env=dict(os.environ)))
