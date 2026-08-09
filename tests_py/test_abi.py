# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The invocation ABI, which is frozen, and the actions implemented on it.

``docs/design/build-container-contract.md`` §5 is the one interface in
this project that can never be changed: a third party writes a build
container against it, in a language nobody here chose, and every future
version of MCUHome has to keep talking to it. That is why this suite is
mostly about *refusals*. A program that builds firmware and answers a
malformed request with a traceback is not usable by a backend; a program
that answers it with exit 66 and nothing on disk is.

Three properties carry most of the file:

* **Exactly one thing produces no result document** — a request that
  cannot be read at all (§5.1 step 4). Everything else, an unimplemented
  request format version included, is a result document, because
  ``result`` is in the immortal preamble.
* **The result document is the last write action, and it is atomic**
  (§5.4). A backend reads it "if it exists, regardless of the exit code"
  (§5.3), so a half-written one is worse than none.
* **A program echoes what it was given, and only that** (§5.4). Inventing
  a ``session`` is what makes an invocation attributable to the wrong one.

Every test arranges what a backend arranges: a per-invocation directory
with the request document in it (§5.1 step 1), never inside a context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from mcuhome import __version__, abi
from mcuhome.context import (
    MANIFEST_FILE,
    ContainerPin,
    ContextFile,
    ContextManifest,
    SdkPin,
    context_id,
)
from mcuhome.contextdir import write_context_manifest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "containers" / "builder" / "Dockerfile"
LAUNCHER = REPO_ROOT / "containers" / "builder" / "run"

#: The fixed absolute path §2.2 gives the program. Restated here so that
#: moving it has to be a deliberate edit in two places.
PROGRAM_PATH = "/mcuhome/run"


class Backend:
    """What a backend does around one invocation (§5.1 steps 1 and 2).

    It owns a per-invocation directory, writes the request document into
    it and names the result file inside it. Nothing here is a fixed path:
    contract v1 defines no mount points (§4), and this suite would not
    notice if it did.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.request = directory / "request.json"
        self.result = directory / "result.json"
        #: No image record by default: the ``subprocess`` profile, and any
        #: filesystem that is not MCUHome's own image.
        self.record = directory / "workspace.json"

    def preamble(self, **fields: Any) -> dict[str, Any]:
        """A request document carrying nothing but the preamble, plus *fields*."""
        return {"request": 1, "result": str(self.result), **fields}

    def run(
        self,
        action: str = "describe",
        document: dict[str, Any] | None = None,
        *,
        text: str | None = None,
        argv: list[str] | None = None,
    ) -> int:
        """Invoke the program and return its exit code.

        *text* writes the request document verbatim, for the documents a
        JSON encoder cannot produce. *argv* replaces the whole command
        line, for the arity rule.
        """
        if text is None:
            text = json.dumps(self.preamble() if document is None else document)
        self.request.write_text(text, encoding="utf-8")
        if argv is None:
            argv = [PROGRAM_PATH, action, str(self.request)]
        return abi.main(argv, record=self.record)

    def document(self) -> dict[str, Any]:
        """The result document, parsed."""
        return json.loads(self.result.read_text(encoding="utf-8"))

    def leftovers(self) -> list[str]:
        """Everything in the invocation directory except what was put there."""
        return sorted(
            entry.name
            for entry in self.directory.iterdir()
            if entry.name not in {self.request.name, self.record.name}
        )


@pytest.fixture
def backend(tmp_path: Path) -> Backend:
    return Backend(tmp_path / "s-42" / "inv-7")


# --------------------------------------------------------------------------
# A locked context to point `verify` at (§3)
# --------------------------------------------------------------------------

#: The resolved pins the contexts below carry. Synthetic on purpose:
#: ``verify`` measures the materialized file set against the integrity
#: list and never looks a pin up — cross-checking them against what was
#: actually pulled is the backend's duty (§9.1) — so nothing here has to
#: name a container or an SDK package that exists.
CONTAINER = ContainerPin(
    image="ghcr.io/mcu-home/build-container",
    tag="zephyr-4.4.0-r4",
    digest="sha256:" + "ab" * 32,
)
SDK = SdkPin(
    constraint="^0.1.0",
    version="0.1.0",
    url="https://example.invalid/mcuhome-sdk-0.1.0.tar.zst",
    sha256="cd" * 32,
)

#: What a context carries here: the canonical device model and one patch,
#: at the two locations §3.1 fixes. The bytes are irrelevant to ``verify``
#: — it hashes whatever is there — and the paths are not. There is no
#: ``keys/signing.pub``, deliberately: §7.2 scopes that file to ``build``.
CONTEXT_FILES = {
    "model/device-model.json": '{"model": 1}\n',
    "patches/zephyr/0001-fix.patch": "--- a\n+++ b\n",
}


def locked_context(root: Path, files: dict[str, str] | None = None) -> ContextManifest:
    """A context directory as ``lock-context`` leaves one (§3.2).

    Written file by file rather than through
    :func:`~mcuhome.contextdir.create_context`, because half of these
    tests need a context no builder would ever produce: one file short,
    one file too many, a manifest that lies about its own ID. What is
    *not* rebuilt here is the ID rule — it comes from
    :func:`~mcuhome.context.context_id`, the same function the program
    under test reaches through, because §3.3 is locked and a second
    implementation of it in a test file would be the first place the two
    sides of the contract could drift apart.
    """
    contents = CONTEXT_FILES if files is None else files
    entries = []
    for name, text in sorted(contents.items()):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        entries.append(ContextFile(path=name, sha256=digest))
    manifest = ContextManifest(
        created="2026-08-09T10:00:00Z",
        sdk=SDK,
        container=CONTAINER,
        board="nrf7002dk/nrf5340/cpuapp",
        files=tuple(entries),
        id=context_id(
            container_digest=CONTAINER.digest,
            sdk_sha256=SDK.sha256,
            board="nrf7002dk/nrf5340/cpuapp",
            files=entries,
        ),
    )
    write_context_manifest(manifest, out_dir=root)
    return manifest


@pytest.fixture
def context(tmp_path: Path) -> Path:
    """A clean locked context, at a path nothing in the contract fixes (§4)."""
    root = tmp_path / "s-42" / "ctx"
    root.mkdir(parents=True)
    locked_context(root)
    return root


# --------------------------------------------------------------------------
# The invocation: two operands, and the one error with no result (§5.1)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operands",
    [
        [],
        ["describe"],
        ["describe", "REQUEST", "--verbose"],
        ["--action", "describe", "REQUEST"],
        ["describe", "REQUEST", "REQUEST"],
    ],
)
def test_any_arity_but_two_operands_is_exit_66(backend: Backend, operands: list[str]) -> None:
    """ "Exactly two positional operands, both mandatory, **never a flag**."

    The argv is frozen and never grows (§5.1), so a program that tolerated
    a third operand today would be tolerating something no backend is
    allowed to send — and the flag forms are here because that is what an
    argument parser bolted on later would accept. The request document
    written below is perfectly good, so arity is the only thing that can
    produce this answer, and nothing is written even though a result path
    was there to be read.
    """
    backend.request.write_text(json.dumps(backend.preamble()), encoding="utf-8")
    resolved = [str(backend.request) if part == "REQUEST" else part for part in operands]
    assert abi.main([PROGRAM_PATH, *resolved], record=backend.record) == abi.EXIT_UNUSABLE
    assert not backend.result.exists()


def test_a_relative_request_path_is_exit_66_and_never_read(
    backend: Backend, tmp_path, monkeypatch
) -> None:
    """§5.1 forbids relying on a ``cwd``, and a relative operand is that.

    The failure this guards against is silent, which is why it is worth a
    test of its own: run from the wrong directory, a relative operand
    either finds nothing or finds a *different* request document and
    answers that one, with exit 0 and a result document a backend has no
    reason to distrust. Here a perfectly good document sits at the
    relative name, so being read is the only way this could pass.
    """
    (tmp_path / "req.json").write_text(json.dumps(backend.preamble()), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert abi.main([PROGRAM_PATH, "describe", "req.json"], record=backend.record) == (
        abi.EXIT_UNUSABLE
    )
    assert not backend.result.exists()


def test_a_request_document_that_is_not_there_is_exit_66(backend: Backend) -> None:
    """§5.1 step 4: the only program-caused error that writes nothing.

    "precisely the case in which the program does not know where a result
    would go" — there is no document to read ``result`` out of.
    """
    argv = [PROGRAM_PATH, "describe", str(backend.directory / "never-written.json")]
    assert abi.main(argv, record=backend.record) == abi.EXIT_UNUSABLE
    assert backend.leftovers() == []


@pytest.mark.parametrize(
    ("what", "text"),
    [
        ("not JSON at all", "{"),
        ("a JSON array", '["describe"]'),
        ("a JSON string", '"describe"'),
        ("empty", ""),
        ("UTF-8 with a BOM", "\ufeff" + '{"request": 1, "result": "/srv/result.json"}'),
    ],
)
def test_a_document_that_is_not_one_json_object_is_exit_66(
    backend: Backend, what: str, text: str
) -> None:
    """§5.2: "UTF-8 without BOM, one JSON object, RFC 8259."

    The BOM case is the one worth spelling out: it parses in several
    languages' JSON readers and not in this one, and the contract names it
    explicitly, so a document carrying it is refused rather than quietly
    accepted here and rejected by the next implementation.
    """
    assert backend.run(text=text) == abi.EXIT_UNUSABLE, what
    assert not backend.result.exists()


def test_a_duplicate_key_is_exit_66(backend: Backend) -> None:
    """ "Duplicate keys are invalid" (§5.2), and this is why it is exit 66.

    A JSON parser that takes the last value would read ``result`` as the
    second of the two below. There is no rule saying which one wins, so
    there is no result document to write and no path to write it to: the
    document does not parse as this contract specifies it.
    """
    text = json.dumps({"request": 1, "result": "/srv/first.json"})
    text = text[:-1] + f', "result": {json.dumps(str(backend.result))}}}'
    assert backend.run(text=text) == abi.EXIT_UNUSABLE
    assert not backend.result.exists()


def test_a_null_anywhere_is_exit_66(backend: Backend) -> None:
    """ "``null`` never means 'absent'; it is invalid" (§5.2).

    Including in a field this program would otherwise ignore: the rule
    governs the document, not the fields one program happens to read, and
    a backend that expresses "no ccache" as ``null`` has to find that out
    from the first program it meets rather than from the third.
    """
    document = backend.preamble(ccache=None)
    assert backend.run(document=document) == abi.EXIT_UNUSABLE
    assert not backend.result.exists()


def test_a_result_that_is_not_an_absolute_path_is_exit_66(backend: Backend) -> None:
    """The one place rule 4 cannot be honoured, so rule 5 applies.

    Rule 4 answers a non-absolute path value with ``unsupported.request``
    — but writing that answer needs an addressable ``result``, and §5.1
    forbids resolving a relative one against a ``cwd``. What is left is
    rule 5's "``result`` is missing or not writable": exit 66, nothing
    written.
    """
    for document in (
        {"request": 1, "result": "result.json"},
        {"request": 1, "result": "./result.json"},
        {"request": 1, "result": 42},
        {"request": 1},
    ):
        assert backend.run(document=document) == abi.EXIT_UNUSABLE, document
    assert backend.leftovers() == []


def test_a_result_nobody_can_write_is_exit_66(backend: Backend) -> None:
    """ "``result`` … not writable" (§5.2 rule 5), found out by writing.

    §5.4 makes the result document the **last** write action, so this
    program does not probe the directory first — it assembles the whole
    document and reports 66 when the write fails. A missing directory is
    the cheapest way to be unwritable and the one that does not depend on
    which user runs the suite.
    """
    document = backend.preamble(result=str(backend.directory / "no-such-dir" / "result.json"))
    assert backend.run(document=document) == abi.EXIT_UNUSABLE
    assert backend.leftovers() == []


def test_the_result_document_is_whole_or_absent(backend: Backend, monkeypatch) -> None:
    """A backend reads the result "if it exists, regardless of the exit code" (§5.3).

    So there is no state in which a partial document exists: the bytes go
    to a temporary file in the same directory and the name appears with a
    rename. This breaks the rename itself — the last step — and asserts
    that the failure leaves neither a result document nor the temporary
    file that carried it.
    """

    def refuse(source, destination):
        raise OSError("the rename failed")

    monkeypatch.setattr(abi.os, "replace", refuse)
    assert backend.run() == abi.EXIT_UNUSABLE
    assert not backend.result.exists()
    assert backend.leftovers() == []


def test_a_successful_invocation_leaves_the_result_and_nothing_else(backend: Backend) -> None:
    """The temporary file is a mechanism, not an artifact.

    It lives in the same directory as the result — it has to, or the
    rename would cross a filesystem and stop being atomic — which is
    exactly the directory a backend inspects afterwards.
    """
    assert backend.run() == abi.EXIT_SUCCESS
    assert backend.leftovers() == ["result.json"]


# --------------------------------------------------------------------------
# The request document: the five parsing rules (§5.2)
# --------------------------------------------------------------------------


def test_an_unimplemented_request_version_is_a_result_document(backend: Backend) -> None:
    """Rule 3, and the whole reason the preamble is immortal.

    "This is always possible because ``result`` is in the immortal
    preamble" — an old program handed a request format version it has
    never heard of still knows where to put its refusal, so a newer
    backend gets a legible answer instead of a silent process.
    """
    assert backend.run(document={"request": 2, "result": str(backend.result)}) == abi.EXIT_FAILURE
    document = backend.document()
    assert document["result"] == 1
    assert document["status"] == "unsupported"
    assert document["reason"] == "unsupported.request"
    assert document["error"]["retryable"] is False


def test_a_request_version_that_is_not_an_integer_is_refused(backend: Backend) -> None:
    """``true`` is an ``int`` in Python and would otherwise read as version 1."""
    for value in (True, "1", 1.0):
        code = backend.run(document={"request": value, "result": str(backend.result)})
        assert code == abi.EXIT_FAILURE, value
        assert backend.document()["reason"] == "unsupported.request"


def test_unknown_fields_at_any_level_are_ignored(backend: Backend) -> None:
    """Rule 1, and the reason the argv never grows.

    "an unknown JSON field is ignored by an older program and costs
    nothing" (§5.1). This is that promise, tested from the older
    program's side: a request carrying a field invented after this
    program was written still gets a successful ``describe``.
    """
    document = backend.preamble(
        **{"x-vendor-mode": "turbo", "limits": {"jobs": 4, "x-nice": 10}, "params": {"mode": "x"}}
    )
    assert backend.run(document=document) == abi.EXIT_SUCCESS
    assert backend.document()["status"] == "success"


def test_a_pointer_this_program_does_not_honour_is_named(backend: Backend) -> None:
    """Rule 2: the list of honoured pointers is explicit, not "is it present".

    ``/params/mode`` is in the document and this program still refuses it,
    because it implements no action that reads it. That is the whole
    point of the rule — "an old program would ignore a top-level field
    that changes the *meaning* of the artifact and then report success".
    """
    document = backend.preamble(params={"mode": "incremental"}, required=["/params/mode"])
    assert backend.run(document=document) == abi.EXIT_FAILURE
    result = backend.document()
    assert result["status"] == "unsupported"
    assert result["reason"] == "unsupported.required"
    assert result["error"]["details"]["required"] == ["/params/mode"]


def test_a_honoured_pointer_with_a_value_that_is_not_is_refused(backend: Backend) -> None:
    """ "knowing the path is not enough: **it must be able to honour the value**".

    ``/session`` is honoured — this program echoes it, which is all §5.2
    permits anyone to do with it — but only as the opaque *string* token
    the contract defines. A backend demanding that a number be honoured
    there is told which pointer failed, not given a result that pretends
    it was.
    """
    document = backend.preamble(session=42, required=["/session"])
    assert backend.run(document=document) == abi.EXIT_FAILURE
    result = backend.document()
    assert result["reason"] == "unsupported.required"
    assert result["error"]["details"]["required"] == ["/session"]


def test_a_pointer_that_resolves_to_nothing_cannot_be_honoured(backend: Backend) -> None:
    """A required field that is not in the document is not honourable either.

    The pointer is one this program knows; there is simply no value at
    it. Answering ``success`` here would be answering that a demand was
    met by its absence.
    """
    document = backend.preamble(required=["/session"])
    assert backend.run(document=document) == abi.EXIT_FAILURE
    assert backend.document()["error"]["details"]["required"] == ["/session"]


def test_every_offending_pointer_is_reported_at_once(backend: Backend) -> None:
    """A backend fixing its request wants the whole list, not the first entry."""
    document = backend.preamble(required=["/out", "/trees/sdk", "/request"])
    assert backend.run(document=document) == abi.EXIT_FAILURE
    assert backend.document()["error"]["details"]["required"] == ["/out", "/trees/sdk"]


def test_a_pointer_this_program_honours_passes(backend: Backend) -> None:
    """The other direction, or the test above would pass on a program that refuses everything."""
    document = backend.preamble(session="s-42", required=["/request", "/result", "/session"])
    assert backend.run(document=document) == abi.EXIT_SUCCESS
    assert backend.document()["reason"] is None


def test_required_that_is_not_a_list_of_pointers_is_unsupported_request(backend: Backend) -> None:
    """Rule 3 and not rule 2, because there is no pointer to name.

    ``error.details.required`` is defined as the offending *pointers*
    (§5.2), and a ``required`` that is not a list of strings has none to
    put there. The document cannot be read as specified, which is what
    ``unsupported.request`` says.
    """
    for value in ("/params/mode", {"0": "/params/mode"}, [1]):
        assert backend.run(document=backend.preamble(required=value)) == abi.EXIT_FAILURE, value
        assert backend.document()["reason"] == "unsupported.request"


def test_a_path_value_that_is_not_absolute_is_unsupported_request(backend: Backend) -> None:
    """Rule 4, stated of the document rather than of one action's fields.

    ``describe`` needs none of these paths, and it refuses anyway: "Every
    path value is absolute" is a property of the request document, and a
    backend that got it wrong for ``out`` has got it wrong for the
    ``build`` that follows.
    """
    for field in ("out", "work", "tmp", "context", "events", "cancel"):
        document = backend.preamble(**{field: f"relative/{field}"})
        assert backend.run(document=document) == abi.EXIT_FAILURE, field
        result = backend.document()
        assert result["status"] == "unsupported"
        assert result["reason"] == "unsupported.request"


def test_a_relative_path_inside_trees_or_ccache_counts_too(backend: Backend) -> None:
    """The path values that are not top-level fields (§4.1, §10).

    A ``trees`` entry is where a backend says which tree it wants built
    against, and a relative one would be resolved against a working
    directory §5.1 forbids relying on. Same for the optional shared cache.
    """
    document = backend.preamble(trees={"zephyr": {"path": "view/zephyr", "writable": True}})
    assert backend.run(document=document) == abi.EXIT_FAILURE
    assert backend.document()["reason"] == "unsupported.request"

    document = backend.preamble(ccache={"path": "ccache", "writable": False})
    assert backend.run(document=document) == abi.EXIT_FAILURE
    assert backend.document()["reason"] == "unsupported.request"


# --------------------------------------------------------------------------
# The result document (§5.4)
# --------------------------------------------------------------------------


def test_the_action_is_echoed_because_it_is_always_present(backend: Backend) -> None:
    """ "``action`` is always echoed, because it is always present — it is ``argv[1]``."

    Including for an action the program refuses: the backend matches the
    refusal against the invocation it made.
    """
    assert backend.run("describe") == abi.EXIT_SUCCESS
    assert backend.document()["action"] == "describe"
    assert backend.run("build") == abi.EXIT_FAILURE
    assert backend.document()["action"] == "build"


def test_a_session_is_echoed_exactly_when_the_request_carried_one(backend: Backend) -> None:
    """The echo rule, in both directions (§5.4).

    ``describe`` "gets by on the preamble alone, so a backend that invokes
    it without [a session] MUST NOT expect it back, and a program MUST NOT
    invent a value for a field it was never given". An invented session is
    how an invocation gets attributed to the wrong one, and the smallest
    conforming ``describe`` — "read two fields, write four" — is only
    possible because the rule is this way round.
    """
    assert backend.run(document=backend.preamble()) == abi.EXIT_SUCCESS
    assert "session" not in backend.document()

    assert backend.run(document=backend.preamble(session="s-42")) == abi.EXIT_SUCCESS
    assert backend.document()["session"] == "s-42"


def test_a_describe_result_reports_nothing_it_did_not_measure(backend: Backend) -> None:
    """The "MUST NOT" row of §5.4's table, all three of it.

    ``context`` because ``describe`` never touches a context and "a
    ``describe`` result carrying one would be reporting a value it could
    not have measured"; ``artifacts`` and ``layers`` because it produces
    none and applies none. The backend compares all three against its own
    values, so a fabricated one is worse than an absent one.
    """
    assert backend.run() == abi.EXIT_SUCCESS
    document = backend.document()
    for forbidden in ("context", "artifacts", "layers"):
        assert forbidden not in document


def test_a_success_carries_a_null_reason_and_a_null_error(backend: Backend) -> None:
    """``error`` is "``null`` otherwise" (§5.4), and ``reason`` with it.

    Present and null rather than absent, as the §5.4 example writes them:
    a consumer reading ``reason`` on every result should not have to tell
    "absent" from "nothing to classify".
    """
    assert backend.run() == abi.EXIT_SUCCESS
    document = backend.document()
    assert document["reason"] is None
    assert document["error"] is None


def test_a_refusal_carries_the_three_subfields_of_the_error_object(backend: Backend) -> None:
    """§5.4.1: ``error`` is a carrier of ``{retryable, message, details}``.

    ``retryable`` is "the program's promise about its own failure, and
    about nothing else" — a refusal to implement an action cannot come out
    differently on a second identical run, so it is false. ``details`` is
    an object even when the contract fixes no contents for it, because it
    is the structured half and a consumer should not have to type-check it
    per reason.
    """
    assert backend.run("build") == abi.EXIT_FAILURE
    error = backend.document()["error"]
    assert error["retryable"] is False
    assert isinstance(error["message"], str) and error["message"]
    assert error["details"] == {}


def test_a_describe_that_refuses_still_says_what_the_program_is(backend: Backend) -> None:
    """§5.4's ``program`` row for ``describe`` carries no qualification.

    "Everything not qualified is mandatory unconditionally" — and unlike
    ``context`` or ``artifacts``, the block reports nothing measured, so a
    refusal can fill it without fabricating anything. It is also the
    moment a backend needs it most: ``program.request`` is what tells it
    which request format version to send instead of the one just refused.

    ``verify`` and ``build`` are the other side of the same table row, a
    MAY, and this program leaves the block out there — asserted by
    :func:`test_an_action_this_program_does_not_implement_refuses_legibly`
    and by :func:`test_a_clean_context_verifies_and_reports_its_own_id`.
    """
    assert backend.run(document={"request": 99, "result": str(backend.result)}) == abi.EXIT_FAILURE
    document = backend.document()
    assert document["reason"] == "unsupported.request"
    assert document["program"]["request"] == [1]
    assert document["program"]["actions"] == ["describe", "verify"]


def test_the_exit_code_and_the_status_say_the_same_thing(backend: Backend) -> None:
    """§5.3: 0 is a result that succeeded, 1 is a result that did not.

    "Where exit code and document contradict each other, the pessimistic
    reading wins **and** a contract violation is raised against the
    image" — so the two are derived from one value here, and this is the
    test that they still are.
    """
    assert backend.run("describe") == abi.EXIT_SUCCESS
    assert backend.document()["status"] == "success"
    assert backend.run("build") == abi.EXIT_FAILURE
    assert backend.document()["status"] == "unsupported"


# --------------------------------------------------------------------------
# The actions (§7)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["build", "x-vendor-thing", ""])
def test_an_action_this_program_does_not_implement_refuses_legibly(
    backend: Backend, action: str
) -> None:
    """§7: ``unsupported.action``, exit 1 — "a legible refusal the backend
    can reschedule on, which is exactly what the removed exit code 64
    could not deliver".

    ``build`` is in the list on purpose. It is one of the three actions
    contract v1 requires of a conforming program and this program does not
    implement it yet; refusing it by name is what keeps that visible
    instead of letting a half-built action report success.
    """
    assert backend.run(action) == abi.EXIT_FAILURE
    document = backend.document()
    assert document["status"] == "unsupported"
    assert document["reason"] == "unsupported.action"
    assert document["action"] == action
    # Nothing measured, so nothing reported (§5.4).
    assert "program" not in document
    for forbidden in ("context", "artifacts", "layers"):
        assert forbidden not in document


def test_describe_fills_every_field_of_the_program_block(backend: Backend) -> None:
    """§7.1.1: "**every field below is mandatory inside it**".

    "A ``describe`` result whose ``program`` block is missing one of them
    is a failed ``describe`` — each field answers a question the backend
    has to answer before it can invoke anything." The values are asserted
    as well as the names, because ``contract``, ``request`` and ``result``
    are the three a backend decides compatibility on and a wrong one is
    not a missing field.
    """
    assert backend.run() == abi.EXIT_SUCCESS
    program = backend.document()["program"]
    assert set(program) == {"id", "version", "contract", "request", "result", "actions", "trees"}
    assert program["id"] == "org.mcuhome.build-container"
    assert program["version"] == __version__
    assert program["contract"] == 1
    assert program["request"] == [1]
    assert program["result"] == [1]


def test_the_action_list_is_what_the_program_implements(backend: Backend) -> None:
    """ "A backend MUST NOT invoke an action absent from the list" (§7.1.1).

    §7.1.1 also requires the list to include ``describe``, ``verify`` and
    ``build``, and both cannot hold for a program that implements two of
    the three. Listing an action that answers ``unsupported.action`` is
    the worse of the two lies: it is the one a backend acts on. So the
    list is the truth, and the image claims no contract conformance
    (``tests_py/test_builder_workspace.py``) until ``build`` exists too.
    """
    assert backend.run() == abi.EXIT_SUCCESS
    assert backend.document()["program"]["actions"] == ["describe", "verify"]


# --------------------------------------------------------------------------
# verify (§7.3)
# --------------------------------------------------------------------------


def verify_request(backend: Backend, context: Path, **fields: Any) -> dict[str, Any]:
    """The two fields a ``verify`` needs, plus whatever a test adds.

    §5.2 makes seven fields mandatory for a working action and this
    program refuses over two of them — see :mod:`mcuhome.abi`'s docstring.
    The other five are added by the tests that are about them, so that
    every test here states its own reason for the fields it sends.
    """
    return backend.preamble(session="s-42", context=str(context), **fields)


def test_a_clean_context_verifies_and_reports_its_own_id(backend: Backend, context: Path) -> None:
    """§7.3: "reports the resulting ``context`` ID in its result".

    The ID is the one the context's own manifest declares, and it has to
    be — the program recomputed it from the bytes on disk, and §3.3 makes
    the two the same value whenever nothing was tampered with. The
    document's whole field set is asserted, because three of §5.4's rows
    are about what a ``verify`` result must *not* carry: ``layers``
    unconditionally ("it reports work that was actually done, and
    ``verify`` does not do that work"), ``artifacts`` because this
    implementation writes no diagnostic output, and ``program`` because
    the block is a MAY outside ``describe``.
    """
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_SUCCESS
    document = backend.document()
    assert set(document) == {"result", "status", "action", "session", "reason", "error", "context"}
    assert document["status"] == "success"
    assert document["action"] == "verify"
    assert document["session"] == "s-42"
    assert document["reason"] is None
    assert document["error"] is None
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", document["context"])


def test_the_reported_id_is_measured_and_not_the_declared_one(
    backend: Backend, context: Path
) -> None:
    """ "computed by the program itself" (§5.4), from the bytes it holds.

    §3.3: "Implementations on both sides of the contract MUST compute the
    ID independently from the bytes they actually hold and MUST NOT trust
    a declared ``id`` value." A program that echoed ``manifest.yaml``'s
    ``id`` would pass every test above and be worthless, since the backend
    compares ``result.context`` against its own measurement (§9.3) — so
    this asserts the value against a manifest the test itself hashed.
    """
    declared = locked_context(context)
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_SUCCESS
    assert backend.document()["context"] == declared.id


def test_a_context_without_a_signing_key_is_not_a_verify_failure(
    backend: Backend, context: Path
) -> None:
    """§7.2: "The requirement is scoped to ``build`` alone."

    "``verify`` compares the materialized file set against the integrity
    list and ``describe`` does not touch the context at all …; neither
    needs a verification key, and neither may refuse a context for the
    absence of one. A context that will only ever be verified or described
    is complete without ``keys/signing.pub``."
    """
    assert not (context / "keys" / "signing.pub").exists()
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_SUCCESS


@pytest.mark.parametrize(
    ("what", "break_it"),
    [
        ("a file whose bytes hash to something else", "tamper"),
        ("a file that is missing", "remove"),
        ("a file present but absent from the list", "smuggle"),
    ],
)
def test_the_three_shapes_of_a_broken_context_are_one_typed_answer(
    backend: Backend, context: Path, what: str, break_it: str
) -> None:
    """§7.3 states all three in one sentence, and one answer for them.

    "A file that is missing, a file whose bytes hash to something else,
    and a file present but absent from the list are one outcome and one
    typed answer: ``status: "failure"``, ``reason:
    "error.context.mismatch"``, the offending paths in ``error.details``."
    It is a ``failure`` and not an ``unsupported``: the program implements
    everything asked of it, and no other image would fare better with this
    context.
    """
    victim = context / "patches" / "zephyr" / "0001-fix.patch"
    if break_it == "tamper":
        victim.write_text("--- a\n+++ EVIL\n", encoding="utf-8")
        offending = "patches/zephyr/0001-fix.patch"
    elif break_it == "remove":
        victim.unlink()
        offending = "patches/zephyr/0001-fix.patch"
    else:
        (context / "patches" / "sdk").mkdir()
        (context / "patches" / "sdk" / "0001-smuggled.patch").write_text("x\n", encoding="utf-8")
        offending = "patches/sdk/0001-smuggled.patch"

    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_FAILURE, what
    document = backend.document()
    assert document["status"] == "failure"
    assert document["reason"] == "error.context.mismatch"
    assert document["error"]["details"]["paths"] == [offending]
    assert document["error"]["retryable"] is False


def test_a_mismatch_still_reports_the_id_it_measured(backend: Backend, context: Path) -> None:
    """§5.4: "An invocation that failed before it got that far reports what
    it measured and nothing more."

    The criterion the paragraph gives for the "MUST, on success" rows is
    measurement, not status: "a ``verify`` that could not read
    ``manifest.yaml`` has no effective context ID" — and one that read it,
    hashed every file and found a disagreement does have one. It is the ID
    of the context *as materialized*, so it is not the declared one, and
    that is exactly the number a backend needs to see next to its own.
    """
    declared = locked_context(context)
    (context / "model" / "device-model.json").write_text('{"model": 2}\n', encoding="utf-8")
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_FAILURE
    document = backend.document()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", document["context"])
    assert document["context"] != declared.id


def test_a_manifest_that_lies_about_its_own_id_does_not_verify(
    backend: Backend, context: Path
) -> None:
    """Every file matches the list, and the context still does not verify.

    §3.3 forbids trusting a declared ``id``, which is only meaningful if
    somebody checks it: every other party in the system recomputes the ID
    and would therefore agree with itself, so a manifest whose ``id`` is
    not the ID of its own contents would travel unnoticed forever. It
    names no offending *file*, because there is none — ``paths`` is empty
    and ``error.message`` carries the two IDs.
    """
    manifest = locked_context(context)
    write_context_manifest(
        ContextManifest(
            created=manifest.created,
            sdk=SDK,
            container=CONTAINER,
            board=manifest.board,
            files=manifest.files,
            id="sha256:" + "00" * 32,
        ),
        out_dir=context,
    )
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_FAILURE
    document = backend.document()
    assert document["reason"] == "error.context.mismatch"
    assert document["error"]["details"]["paths"] == []
    assert "context id" in document["error"]["message"]


def test_a_context_without_a_manifest_is_incomplete(backend: Backend, context: Path) -> None:
    """§3.1: "``manifest.yaml`` is the program's entry point".

    So a context that carries none is "missing a file the action needs",
    which §5.4's registry answers with ``error.context.incomplete`` and
    "the missing path in ``error.details``". Nothing was measured, so
    nothing is reported: there is no ``context`` ID in this document, and
    §5.4 says why — "Fabricating either would be worse than omitting it,
    since the backend compares both against its own values".
    """
    (context / MANIFEST_FILE).unlink()
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_FAILURE
    document = backend.document()
    assert document["status"] == "failure"
    assert document["reason"] == "error.context.incomplete"
    assert document["error"]["details"]["missing"] == [MANIFEST_FILE]
    assert "context" not in document


def test_a_context_directory_that_is_not_there_is_the_same_absence(
    backend: Backend, context: Path
) -> None:
    """From the program's side a missing directory and a missing manifest
    are one thing: the file it has to start from is not there."""
    document = verify_request(backend, context / "does-not-exist")
    assert backend.run("verify", document) == abi.EXIT_FAILURE
    assert backend.document()["reason"] == "error.context.incomplete"


def test_a_context_format_version_this_program_does_not_implement(
    backend: Backend, context: Path
) -> None:
    """§3.2, the one context problem that is ``unsupported`` and not a failure.

    "A program MUST check the ``context`` format version and, for a
    version it does not implement, fail the invocation with ``status:
    "unsupported"``, ``reason: "unsupported.context"`` … and the version
    it found in ``error.details``. It is ``unsupported`` and not
    ``failure`` … because the program is refusing a document written to a
    specification it does not have, which a backend can act on by choosing
    another image — nothing about this context is broken." Nothing is
    hashed on the way out either: the rule for hashing a version 2 context
    is written in a document this program does not have.
    """
    path = context / MANIFEST_FILE
    path.write_text(
        path.read_text(encoding="utf-8").replace("context: 1", "context: 2"), encoding="utf-8"
    )
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_FAILURE
    document = backend.document()
    assert document["status"] == "unsupported"
    assert document["reason"] == "unsupported.context"
    assert document["error"]["details"]["context"] == 2
    assert "context" not in document


def test_a_format_version_that_is_not_json_is_still_reported(
    backend: Backend, context: Path
) -> None:
    """The result document is "the **last write action** of the invocation"
    (§5.4), so it must never be the thing that fails.

    ``context: 2026-08-09`` is a date to a YAML parser, and a date is not
    something :func:`json.dumps` can write. Passing it through would cost
    this invocation its whole answer — nothing written and exit 66 (§5.2
    rule 5), for a context the program diagnosed perfectly well. So the
    value reaches ``error.details`` as text, which is all a backend needs
    to see what it sent.
    """
    path = context / MANIFEST_FILE
    path.write_text(
        path.read_text(encoding="utf-8").replace("context: 1", "context: 2026-08-09"),
        encoding="utf-8",
    )
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_FAILURE
    document = backend.document()
    assert document["reason"] == "unsupported.context"
    assert document["error"]["details"]["context"] == "2026-08-09"


def test_a_manifest_that_states_no_format_version_is_not_unsupported(
    backend: Backend, context: Path
) -> None:
    """A broken manifest, and ``unsupported.context`` would say it is not.

    That reason means the program "found a ``context`` format version it
    does not implement" (§5.4) and §3.2 justifies the status with "nothing
    about this context is broken". A manifest with no ``context`` key is
    broken, and telling a backend to go and find another image for it
    would send it looking for something no image has. So it is the failure
    every unreadable manifest is — see :mod:`mcuhome.abi`'s docstring for
    the gap in contract v1 this stands in.
    """
    path = context / MANIFEST_FILE
    kept = [
        line
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
        if not line.startswith("context:")
    ]
    path.write_text("".join(kept), encoding="utf-8")
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_FAILURE
    document = backend.document()
    assert document["status"] == "failure"
    assert document["reason"] == "error.context.mismatch"


@pytest.mark.parametrize(
    ("what", "text"),
    [
        ("not YAML at all", "files: [unclosed\n"),
        ("not a mapping", "- one\n- two\n"),
        ("a manifest missing a section", "context: 1\nid: sha256:x\n"),
        (
            "a hash spelled a second way",
            "context: 1\ncreated: 2026-08-09T10:00:00Z\n"
            "mcuhome:\n  constraint: '^0.1.0'\n  version: 0.1.0\n"
            "  package: {url: 'https://example.invalid/x', sha256: " + "CD" * 32 + "}\n"
            "container: {image: i, tag: t, digest: 'sha256:" + "ab" * 32 + "'}\n"
            "target: {board: nrf7002dk/nrf5340/cpuapp}\nfiles: []\nid: 'sha256:"
            + "ab" * 32
            + "'\n",
        ),
    ],
)
def test_a_manifest_that_cannot_be_read_is_a_failure(
    backend: Backend, context: Path, what: str, text: str
) -> None:
    """Contract v1 types none of these, and this is where they land.

    §3.3.1 requires the last case — one spelling per hash — to be refused
    rather than normalized: "An implementation that encounters one MUST
    refuse the manifest, naming the offending value, and MUST NOT compute
    an ID from it." Which is also why no ``context`` comes back from any
    of them: nothing was measured.
    """
    (context / MANIFEST_FILE).write_text(text, encoding="utf-8")
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_FAILURE, what
    document = backend.document()
    assert document["status"] == "failure"
    assert document["reason"] == "error.context.mismatch"
    assert "context" not in document
    assert document["error"]["details"]["paths"] == []


def test_a_verify_needs_a_context_and_a_session(backend: Backend, context: Path) -> None:
    """Rule 3 (§5.2), over the two fields this action actually needs.

    ``context`` because §7.3 defines ``verify`` over it. ``session``
    because §5.4 makes it mandatory in a ``verify`` result *and* forbids
    inventing one — "a program MUST NOT invent a value for a field it was
    never given" — so a ``verify`` without it has no conforming result to
    write at all, and refusing is the only answer left.
    """
    for document in (
        backend.preamble(session="s-42"),
        backend.preamble(context=str(context)),
        backend.preamble(session="s-42", context=42),
    ):
        assert backend.run("verify", document) == abi.EXIT_FAILURE, document
        result = backend.document()
        assert result["status"] == "unsupported"
        assert result["reason"] == "unsupported.request"
        assert "context" not in result


def test_a_verify_does_not_demand_the_fields_it_does_not_use(
    backend: Backend, context: Path
) -> None:
    """§4.1: "The program MUST NOT require an entry it does not need for
    the requested action."

    §5.2 makes ``out``, ``work``, ``tmp``, ``trees.sdk`` and
    ``limits.jobs`` mandatory for every working action, and a backend that
    omits them is in breach of it — but this ``verify`` can answer
    correctly without them, and §7.3 says why: it "reads the context and
    nothing else". Refusing here would report the backend's defect by
    withholding an answer this program already has.
    """
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_SUCCESS
    assert backend.document()["status"] == "success"


def test_the_context_pointer_is_honoured_and_the_unused_ones_are_not(
    backend: Backend, context: Path
) -> None:
    """§5.2 rule 2, on the fields a conforming backend sends every time.

    ``/context`` is honoured because the program acts on the value: it
    opens that directory. ``/trees/sdk`` is not, although a conforming
    request always carries it, because a ``verify`` does nothing with a
    source tree (§7.3, §9.2 point 10) — "a program that knows
    ``/params/mode`` but not the value ``reproducible`` MUST refuse with
    ``unsupported.required`` rather than accept the job and quietly
    deliver something else", and a promise to honour a value nothing reads
    is that same lie in its cheapest form.
    """
    honoured = verify_request(backend, context, required=["/context", "/session", "/result"])
    assert backend.run("verify", honoured) == abi.EXIT_SUCCESS

    trees = {"sdk": {"path": str(context), "writable": False}}
    refused = verify_request(backend, context, trees=trees, required=["/trees/sdk"])
    assert backend.run("verify", refused) == abi.EXIT_FAILURE
    document = backend.document()
    assert document["reason"] == "unsupported.required"
    assert document["error"]["details"]["required"] == ["/trees/sdk"]


def test_a_verify_writes_nothing_but_its_result_document(
    backend: Backend, context: Path, tmp_path: Path
) -> None:
    """§9.2 point 10: no patch, no write into a ``trees`` entry, no ``work``.

    §7.3 is where the reason is: a ``verify`` that patched "would consume
    the 'applied once per session' budget before the first build, it would
    make a read-only check into a writing one, and it would make a cheap
    pre-flight check cost a patch application". So the full set of paths a
    conforming backend hands over is passed here, every one of them
    writable, and every one of them has to come back untouched — the
    context included, because §3.1 makes it "a read-only input for the
    whole life of a session".
    """
    areas = {name: tmp_path / "s-42" / name for name in ("out", "work", "tmp", "view")}
    for path in areas.values():
        path.mkdir(parents=True)
    before = sorted(path.relative_to(context).as_posix() for path in context.rglob("*"))

    document = verify_request(
        backend,
        context,
        out=str(areas["out"]),
        work=str(areas["work"]),
        tmp=str(areas["tmp"]),
        trees={"zephyr": {"path": str(areas["view"]), "writable": True}},
        limits={"jobs": 4},
    )
    assert backend.run("verify", document) == abi.EXIT_SUCCESS
    for name, path in areas.items():
        assert list(path.iterdir()) == [], f"a verify wrote into {name}"
    assert sorted(p.relative_to(context).as_posix() for p in context.rglob("*")) == before


# --------------------------------------------------------------------------
# What describe says about the trees (§7.1.1)
# --------------------------------------------------------------------------


def test_without_a_record_every_tree_is_the_backends_to_supply(backend: Backend) -> None:
    """``describe`` answers even where there is no image at all.

    The ``subprocess`` profile has no image, and a ``path`` of ``null``
    is the contract's own way of saying "this tree is not in my image; put
    it wherever you like and name it in ``trees``". That answer is one
    every backend can satisfy, which is why an unreadable record is
    treated as an absent one rather than as a failed ``describe``.
    """
    assert backend.run() == abi.EXIT_SUCCESS
    trees = backend.document()["program"]["trees"]
    assert trees == {name: {"path": None} for name in ("zephyr", "sdk", "chip", "mcuboot")}


def test_the_trees_come_from_the_images_own_record(backend: Backend) -> None:
    """ "the only way a backend learns where a foreign image keeps its trees".

    Without it §6.2's writable views "cannot be arranged at all". The
    image writes ``/mcuhome/workspace.json`` at build time
    (``containers/builder/workspace-record.py``) under the contract's own
    layer names, so this is a lookup and not a translation — and the
    ``mounted`` flag becomes the ``null`` path, because the SDK is a
    hash-pinned package fetched per session (ADR 0018) and its directory
    is the backend's to name.
    """
    backend.record.write_text(
        json.dumps(
            {
                "workspace": 1,
                "layers": {
                    "zephyr": {"path": "/mcuhome/workspace/zephyr", "revision": "v4.4.0"},
                    "chip": {"path": "/mcuhome/workspace/modules/lib/connectedhomeip"},
                    "sdk": {"path": "/mcuhome/workspace/mcuhome", "mounted": True},
                    "x-vendor": {"path": "/opt/vendor", "revision": "3"},
                },
            }
        ),
        encoding="utf-8",
    )
    assert backend.run() == abi.EXIT_SUCCESS
    assert backend.document()["program"]["trees"] == {
        "zephyr": {"path": "/mcuhome/workspace/zephyr", "version": "v4.4.0"},
        "sdk": {"path": None},
        "chip": {"path": "/mcuhome/workspace/modules/lib/connectedhomeip"},
        "mcuboot": {"path": None},
        "x-vendor": {"path": "/opt/vendor", "version": "3"},
    }


# --------------------------------------------------------------------------
# The program the image installs (§2.2)
# --------------------------------------------------------------------------


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_the_image_installs_the_program_at_the_fixed_absolute_path() -> None:
    """§2.2: ``/mcuhome/run``, and "the program is **not** looked up on ``PATH``".

    Three properties of the filesystem the contract does not control make
    the path absolute: ``PATH`` is the image author's to set, ``docker
    exec`` inherits the environment fixed at container creation, and the
    invocation is resolved without a shell. So there is nothing to fall
    back on, and the ``COPY`` destination is the interface.
    """
    assert f"COPY containers/builder/run {PROGRAM_PATH}" in _dockerfile()


def test_the_program_is_executable_by_every_user_the_backend_may_exec_as() -> None:
    """§2.2 requires exactly that, and it is why the mode is set explicitly.

    "the backend runs the program as the calling user where it can" — a
    user that owns nothing in this image and has no entry in its
    ``/etc/passwd``. A file carrying whatever mode git recorded would be
    executable for whoever built the image.
    """
    assert re.search(rf"^RUN chmod 0755 {PROGRAM_PATH}$", _dockerfile(), re.MULTILINE)


def test_the_program_is_copied_after_the_namespace_it_lives_in() -> None:
    """``COPY --from=workspace /mcuhome /mcuhome`` lands the whole namespace.

    Copied before that, the launcher would be replaced by the baked
    workspace's copy of ``/mcuhome`` — which does not contain one — and
    the image would ship without a program while every line that installs
    it is still in the file.
    """
    text = _dockerfile()
    assert text.index("COPY --from=workspace /mcuhome /mcuhome") < text.index(
        f"COPY containers/builder/run {PROGRAM_PATH}"
    )


def test_the_launcher_is_a_launcher() -> None:
    """ "a script or binary" (§2.2) — and this one is thin on purpose.

    The ABI lives in :mod:`mcuhome.abi` because the ``subprocess`` profile
    runs the same code with no image and no launcher around it (§1.2). If
    this file ever grows a second job, the two profiles stop being one
    implementation.
    """
    text = LAUNCHER.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "exec" in text and "mcuhome.abi" in text
    assert LAUNCHER.suffix == "", "§2.2: no extension — a third party may ship a binary here"
    assert os.access(LAUNCHER, os.X_OK), "the file in the repository is executable"
