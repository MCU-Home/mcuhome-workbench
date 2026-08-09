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
from conftest import VALID_CONFIG, resolve_file

from mcuhome.compiler import abi
from mcuhome.model import __version__
from mcuhome.model.context import (
    MANIFEST_FILE,
    ContainerPin,
    ContextFile,
    ContextManifest,
    SdkPin,
    context_id,
)
from mcuhome.model.errors import BuildError
from mcuhome.workbench.contextdir import write_context_manifest

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
        env: dict[str, str] | None = None,
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
        return abi.main(argv, record=self.record, env=env)

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
    :func:`~mcuhome.workbench.contextdir.create_context`, because half of these
    tests need a context no builder would ever produce: one file short,
    one file too many, a manifest that lies about its own ID. What is
    *not* rebuilt here is the ID rule — it comes from
    :func:`~mcuhome.model.context.context_id`, the same function the program
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
    assert backend.run("generate") == abi.EXIT_FAILURE
    assert backend.document()["action"] == "generate"


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
    assert backend.run("generate") == abi.EXIT_FAILURE
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
    assert document["program"]["actions"] == ["describe", "verify", "build"]


def test_the_exit_code_and_the_status_say_the_same_thing(backend: Backend) -> None:
    """§5.3: 0 is a result that succeeded, 1 is a result that did not.

    "Where exit code and document contradict each other, the pessimistic
    reading wins **and** a contract violation is raised against the
    image" — so the two are derived from one value here, and this is the
    test that they still are.
    """
    assert backend.run("describe") == abi.EXIT_SUCCESS
    assert backend.document()["status"] == "success"
    assert backend.run("generate") == abi.EXIT_FAILURE
    assert backend.document()["status"] == "unsupported"


# --------------------------------------------------------------------------
# The actions (§7)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["generate", "x-vendor-thing", ""])
def test_an_action_this_program_does_not_implement_refuses_legibly(
    backend: Backend, action: str
) -> None:
    """§7: ``unsupported.action``, exit 1 — "a legible refusal the backend
    can reschedule on, which is exactly what the removed exit code 64
    could not deliver".

    ``generate`` is in the list on purpose, and it is the one action that
    can never move out of it: §6.1 makes it "an action of the SDK entry
    point and **not** of the program: it is never invoked on
    ``/mcuhome/run`` and never appears in ``program.actions``". A backend
    that mistook the two would otherwise get a build container generating
    for itself out of a tree it was not handed.
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
    ``build``, and all three are here now — but the list is asserted
    against :data:`~mcuhome.compiler.abi.IMPLEMENTED_ACTIONS` and not against the
    contract's sentence, because a list that ran ahead of the code is the
    one lie a backend acts on. ``generate`` stays out of it forever
    (§6.1).
    """
    assert backend.run() == abi.EXIT_SUCCESS
    actions = backend.document()["program"]["actions"]
    assert actions == list(abi.IMPLEMENTED_ACTIONS) == ["describe", "verify", "build"]
    assert "generate" not in actions


# --------------------------------------------------------------------------
# verify (§7.3)
# --------------------------------------------------------------------------


def verify_request(backend: Backend, context: Path, **fields: Any) -> dict[str, Any]:
    """The two fields a ``verify`` needs, plus whatever a test adds.

    §5.2 makes seven fields mandatory for a working action and this
    program refuses over two of them — see :mod:`mcuhome.compiler.abi`'s docstring.
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
    broken — §7.3 says it in as many words since the E36 erratum: "no
    other image would fare better with it, so there is nothing for a
    backend to reschedule onto". So it is ``error.context.unreadable``,
    the reason the registry provides for every manifest that cannot be
    read as one.
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
    assert document["reason"] == "error.context.unreadable"


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
    """``error.context.unreadable`` — the registry row these cases got.

    "found ``manifest.yaml`` and cannot read it as one: broken YAML, a
    missing section, or a hash in a spelling §3.3.1 refuses" (§5.4). The
    reason is distinct from ``mismatch`` on purpose: a backend can tell
    a corrupt manifest from a tampered context file without parsing
    untrusted message text. §3.3.1 requires the last case — one spelling
    per hash — to be refused rather than normalized, and no ``context``
    comes back from any of them: nothing was measured.
    """
    (context / MANIFEST_FILE).write_text(text, encoding="utf-8")
    assert backend.run("verify", verify_request(backend, context)) == abi.EXIT_FAILURE, what
    document = backend.document()
    assert document["status"] == "failure"
    assert document["reason"] == "error.context.unreadable"
    assert "context" not in document
    assert document["error"]["details"] == {}


def test_a_verify_needs_a_context_and_a_session(backend: Backend, context: Path) -> None:
    """Rule 3 (§5.2), over the two fields this action actually needs.

    ``context`` because §7.3 defines ``verify`` over it. ``session``
    because §5.4 has a conforming invocation carry it — the row is
    conditional on §5.2 making the request carry it, so a request
    without it is the backend's breach, and refusing under rule 3 is how
    this program reports a breach instead of inventing the echo.
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
# build (§7.2)
# --------------------------------------------------------------------------

#: A sysbuild log carrying one memory report per image, each preceded by
#: the build-step banner that is the only thing in a sysbuild log saying
#: whose output follows (``mcuhome/compiler/workspace.py:838``). The report itself
#: names no image, which is exactly why the banner has to be there.
BUILD_LOG = """\
[1/2] Performing build step for 'mcuboot'
Memory region         Used Size  Region Size  %age Used
           FLASH:       49152 B        64 KB     75.00%
[2/2] Performing build step for 'app'
Memory region         Used Size  Region Size  %age Used
           FLASH:      859672 B         1 MB     81.99%
"""

#: What the built application's ``.config`` says. imgtool's ``--version``
#: is the one of the four signing arguments the builder does not itself
#: decide, and ``CONFIG_ROM_START_OFFSET`` is the header offset the image
#: was really linked with — ``mcuhome/compiler/report.py`` cross-checks it against
#: the board's layout, so it has to be the registry's 512 here.
BUILD_KCONFIG = 'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="1.4.0+0"\nCONFIG_ROM_START_OFFSET=0x200\n'

#: The board every fixture below builds for, and the four ``imgtool sign``
#: arguments ``mcuhome/model/registry.py`` states for it.
BOARD = "nrf7002dk/nrf5340/cpuapp"
SIGNING_ARGUMENTS = {"version": "1.4.0+0", "header-size": 512, "align": 4, "slot-size": 933888}

#: What an SDK package declares at the root of ``trees.sdk`` (§6.1).
#: Contract v1 fixes the file name and these three field names, and no
#: values — ``runtime`` is "an opaque string" and is never interpreted.
SDK_METADATA = {"sdk": 1, "generate": {"program": "bin/generate", "runtime": "py"}}

#: §5.4's patchset encoding over the two patches below, computed once
#: from the stated rule rather than by running the implementation.
PATCHSET_OF_TWO = "sha256:e3078d6fac12ec834a2359fff0ebcfbcfecb38524e6d2f765c1a75d8f73e6a98"


@pytest.fixture(scope="session")
def device_model_json(tmp_path_factory) -> str:
    """The canonical model a locked context carries, as JSON.

    Resolved from a real configuration rather than hand-written: §3.1 puts
    ``model/device-model.json`` in the context and ``mcuhome.workbench.api.read_model``
    refuses anything that is not one, so a fixture that faked it would test
    the fake.
    """
    root = tmp_path_factory.mktemp("device")
    (root / "main.yaml").write_text(VALID_CONFIG, encoding="utf-8")
    return resolve_file(root / "main.yaml").to_json()


class BuildSetup:
    """Everything a backend arranges around one ``build`` (§5.2, §6, §9.1).

    A per-invocation directory, an emptied ``out``, a session-persistent
    ``work``, a scratch ``tmp``, a locked context with the verification
    key §7.2 demands, and a west workspace the program can claim as its
    own — plus the two child processes stubbed out, because a real Matter
    compile is a quarter of an hour and this suite promises one second.

    Nothing here is at a path the contract fixes: "Contract v1 defines no
    mount points" (§4), and every path below is chosen by this fixture.
    """

    def __init__(self, backend: Backend, root: Path, model_json: str, monkeypatch) -> None:
        self.backend = backend
        self.root = root
        self.context = root / "ctx"
        self.context.mkdir(parents=True)
        self.files = {
            "model/device-model.json": model_json,
            "keys/signing.pub": "-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n",
        }
        self.manifest = locked_context(self.context, self.files)

        self.out = root / "inv-7" / "out"
        self.work = root / "work"
        self.tmp = root / "inv-7" / "tmp"
        for path in (self.out, self.work, self.tmp):
            path.mkdir(parents=True)

        self.workspace_dir = root / "ws"
        self.sdk = self.workspace_dir / "mcuhome"
        self.layers = {
            "zephyr": self.workspace_dir / "zephyr",
            "chip": self.workspace_dir / "modules" / "lib" / "connectedhomeip",
            "mcuboot": self.workspace_dir / "bootloader" / "mcuboot",
            "sdk": self.sdk,
        }
        for path in self.layers.values():
            path.mkdir(parents=True)
        (self.sdk / "bin").mkdir()
        (self.sdk / "bin" / "generate").write_text("#!/bin/sh\n", encoding="utf-8")
        self.write_sdk_metadata(SDK_METADATA)
        backend.record.write_text(
            json.dumps(
                {
                    "workspace": 1,
                    "topdir": str(self.workspace_dir),
                    "manifest": {"path": "mcuhome", "file": "west.yml"},
                    "layers": {
                        "zephyr": {"path": str(self.layers["zephyr"])},
                        "chip": {"path": str(self.layers["chip"])},
                        "mcuboot": {"path": str(self.layers["mcuboot"])},
                        "sdk": {"path": str(self.sdk), "mounted": True},
                    },
                }
            ),
            encoding="utf-8",
        )

        #: The environment the backend states (§6.1: ``PATH`` "arrives with
        #: the environment the caller stated"). Three empty executables are
        #: enough for :func:`~mcuhome.compiler.workspace.require_tools`, which asks
        #: whether the tools *can start*, not whether they work — the
        #: compile behind them is stubbed anyway. A test that wants the
        #: typed missing-tool refusal passes ``env={}`` instead.
        bindir = root / "bin"
        bindir.mkdir()
        for tool in ("west", "gn", "zap"):
            executable = bindir / tool
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        self.env = {"PATH": str(bindir)}

        #: Every child process the program started, in order.
        self.children: list[dict[str, Any]] = []
        #: Every :class:`~mcuhome.compiler.workspace.BuildPlan` it would have run.
        self.plans: list[Any] = []
        self.child_code = 0
        self.build_code = 0
        #: The failure modes §6.1 and §7.2 name, drivable one at a time:
        #: a generate child that dies without a result document, one that
        #: answers a non-``success``, a compiler that cannot even start,
        #: and a sysbuild that leaves the tree §7.2.1 cannot report from.
        self.generate_writes_result = True
        self.generate_status = "success"
        self.build_raises: str | None = None
        self.omit_app_hex = False
        self.omit_config = False
        monkeypatch.setattr(abi, "_run_child", self._run_child)
        monkeypatch.setattr(abi.workspace, "run_build", self._run_build)

    # -- arranging ---------------------------------------------------------

    def write_sdk_metadata(self, document: Any) -> None:
        (self.sdk / abi.SDK_METADATA_FILE).write_text(json.dumps(document), encoding="utf-8")

    def patch(self, layer: str, name: str, body: str) -> None:
        """Put a patch into the context and re-lock it over the new file set."""
        path = self.context / "patches" / layer / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.files[f"patches/{layer}/{name}"] = body
        self.manifest = locked_context(self.context, self.files)

    def trees(self, **writable: str) -> dict[str, Any]:
        """``trees``: ``sdk`` always, plus the writable views a test asks for."""
        entries: dict[str, Any] = {"sdk": {"path": str(self.sdk), "writable": False}}
        for layer in writable:
            entries[layer] = {"path": str(self.layers[layer]), "writable": True}
        return entries

    def request(self, **fields: Any) -> dict[str, Any]:
        """The seven fields §5.2 makes mandatory for a working action."""
        document = self.backend.preamble(
            session="s-42",
            out=str(self.out),
            work=str(self.work),
            tmp=str(self.tmp),
            context=str(self.context),
            trees=self.trees(),
            limits={"jobs": 4},
        )
        document.update(fields)
        return document

    def run(self, **fields: Any) -> int:
        return self.backend.run("build", self.request(**fields), env=self.env)

    def document(self) -> dict[str, Any]:
        return self.backend.document()

    # -- the two stubbed children ------------------------------------------

    def _run_child(self, command, *, env, directory):
        """Stands in for ``git apply`` and for the SDK's ``generate``.

        The generate half is a real backend of §5.1's ABI seen from the
        other side: it reads the request document the program wrote, puts
        an application tree where that document says, and answers with a
        result document. Anything less would let the program's own
        checking of that answer go untested.
        """
        record = {"command": list(command), "env": dict(env), "directory": Path(directory)}
        if len(command) == 3 and command[1] == abi.GENERATE_ACTION:
            handed = Path(json.loads(Path(command[2]).read_text(encoding="utf-8"))["out"])
            # What the child sees the moment it starts — asserted empty by
            # the invocation test, because §4 promises it emptiness.
            record["out_entries"] = sorted(p.name for p in handed.iterdir())
        self.children.append(record)
        if len(command) == 3 and command[1] == abi.GENERATE_ACTION:
            request = json.loads(Path(command[2]).read_text(encoding="utf-8"))
            tree = Path(request["out"]) / "app"
            tree.mkdir(parents=True, exist_ok=True)
            (tree / "CMakeLists.txt").write_text("# generated\n", encoding="utf-8")
            if self.generate_writes_result:
                Path(request["result"]).write_text(
                    json.dumps(
                        {
                            "result": 1,
                            "status": self.generate_status,
                            "action": abi.GENERATE_ACTION,
                            "session": request["session"],
                            "reason": None if self.generate_status == "success" else "x-test.made",
                            "error": None,
                        }
                    ),
                    encoding="utf-8",
                )
        return self.child_code, "" if self.child_code == 0 else "the child refused"

    def _run_build(self, plan, *, stream=None):
        """Stands in for stage 5, leaving behind exactly what sysbuild does."""
        self.plans.append(plan)
        if self.build_raises is not None:
            raise BuildError(self.build_raises)
        if self.build_code != 0:
            return self.build_code, BUILD_LOG
        for image in ("app", "mcuboot"):
            output = plan.build_dir / image / "zephyr"
            output.mkdir(parents=True, exist_ok=True)
            if not (image == "app" and self.omit_app_hex):
                (output / "zephyr.hex").write_text(f":00000001FF {image}\n", encoding="utf-8")
            (output / "zephyr.bin").write_bytes(image.encode("utf-8"))
        if not self.omit_config:
            (plan.build_dir / "app" / "zephyr" / ".config").write_text(
                BUILD_KCONFIG, encoding="utf-8"
            )
        return 0, BUILD_LOG


@pytest.fixture
def build(backend: Backend, tmp_path: Path, device_model_json: str, monkeypatch) -> BuildSetup:
    return BuildSetup(backend, tmp_path / "s-42", device_model_json, monkeypatch)


def test_a_successful_build_declares_the_firmware_and_the_report(build: BuildSetup) -> None:
    """§7.2's floor: "at least two artifacts … and **exactly one artifact
    with role ``report``**".

    "The report is mandatory because the program is forbidden to sign and
    the client therefore has to: a build whose parameters the client
    cannot read produces an image nobody can sign." The bootloader is
    declared on top of the floor and sysbuild's combined hex is not — see
    :mod:`mcuhome.compiler.abi`'s docstring for both.
    """
    assert build.run() == abi.EXIT_SUCCESS
    artifacts = build.document()["artifacts"]
    assert [(entry["role"], entry["path"]) for entry in artifacts] == [
        ("firmware", "firmware.hex"),
        ("firmware", "firmware.bin"),
        ("bootloader", "bootloader.hex"),
        ("report", "build-report.json"),
    ]
    assert [entry["role"] for entry in artifacts].count("report") == 1


def test_a_build_result_carries_exactly_the_rows_five_four_makes_mandatory(
    build: BuildSetup,
) -> None:
    """§5.4's table, the ``build`` column, read as a whole.

    ``result``, ``status``, ``action``, ``session``, ``reason``/``error``
    unconditionally; ``context``, ``artifacts`` and ``layers`` on success.
    ``program`` is a MAY outside ``describe`` and this program omits it.
    ``layers`` is present and empty here because this context patches
    nothing: the row says "for every patched layer", and none is none.
    """
    assert build.run() == abi.EXIT_SUCCESS
    document = build.document()
    assert set(document) == {
        "result",
        "status",
        "action",
        "session",
        "reason",
        "error",
        "context",
        "artifacts",
        "layers",
    }
    assert document["action"] == "build"
    assert document["session"] == "s-42"
    assert document["reason"] is None and document["error"] is None
    assert document["layers"] == {}


def test_every_declared_artifact_exists_and_re_hashes_to_its_declared_value(
    build: BuildSetup,
) -> None:
    """§5.3's success criterion, checked the way the backend checks it.

    "every declared artifact exists as a regular file under its declared
    ``root`` and re-hashes to its declared value" — declared hash against
    file content, which is what a backend can measure. What this
    deliberately does *not* claim to prove is §5.4's "read back from disk
    after ``fsync``, never taken from a buffer": buffer and file hold the
    same bytes here, so no re-hash can tell the two apart — that rule is
    kept by the implementation's shape, not by this test. ``root`` is
    ``"out"``, "in v1 … exactly one legal value", and the path segments
    match ``[A-Za-z0-9._-]+``.
    """
    assert build.run() == abi.EXIT_SUCCESS
    for entry in build.document()["artifacts"]:
        assert entry["root"] == "out"
        assert re.fullmatch(r"[A-Za-z0-9._-]+", entry["path"])
        path = build.out / entry["path"]
        assert path.is_file() and not path.is_symlink()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["hashes"] == {"sha256": digest}
        assert re.fullmatch(r"[0-9a-f]{64}", entry["hashes"]["sha256"])
        assert "size" not in entry


def test_the_build_report_is_the_document_seven_two_one_describes(build: BuildSetup) -> None:
    """§7.2.1, field for field, because one consumer signs from it.

    ``signing`` is MANDATORY and carries the key type MCUboot was
    configured to verify with plus "the **four ``imgtool sign``
    arguments**, under imgtool's own option names". ``memory`` is
    "optional, one entry per memory region of a linked image, with exactly
    the fields of the ``build.memory.region`` event". Nothing of
    ``build-manifest.json``'s ``signed``, ``signed_by_the_build``,
    ``inputs`` or ``outputs`` is here: the contract strikes all four.
    """
    assert build.run() == abi.EXIT_SUCCESS
    report = json.loads((build.out / "build-report.json").read_text(encoding="utf-8"))
    assert report["report"] == 1
    assert report["signing"]["signature_type"] == "ecdsa-p256"
    assert report["signing"]["arguments"] == SIGNING_ARGUMENTS
    assert report["memory"] == [
        {"image": "mcuboot", "region": "FLASH", "used": 49152, "total": 65536, "percent": 75.0},
        {"image": "app", "region": "FLASH", "used": 859672, "total": 1048576, "percent": 81.99},
    ]
    assert set(report) == {"report", "signing", "memory"}


def test_a_build_never_signs_and_verifies_against_the_contexts_own_key(
    build: BuildSetup,
) -> None:
    """§7.2: "The program MUST NOT sign images … It MUST use
    ``keys/signing.pub`` from the context as the bootloader's verification
    key and MUST NOT fall back to a default signing key."

    The default it must not fall back to is MCUboot's demo key, "**its
    private half is published**" — so the assertion is not that some key
    was passed but that *this* file was, and that the build ran with
    detached signing on, which is what stops any signing step from
    running at all. That flag pair is the whole check on purpose:
    ``out`` is a closed whitelist of names this program writes, so
    "nothing signed in ``out``" would pass whatever the build did, and
    an assertion that cannot fail proves nothing.
    """
    assert build.run() == abi.EXIT_SUCCESS
    command = build.plans[0].command
    assert f'-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="{build.context / "keys" / "signing.pub"}"' in (
        command
    )
    assert "-DMCUHOME_DETACHED_SIGNING=y" in command


def test_a_context_without_a_signing_key_fails_the_invocation_typed(build: BuildSetup) -> None:
    """§7.2: the absence is an error, never a default.

    "A context submitted to a ``build`` that does not carry it fails the
    invocation typed — ``status: "failure"``, ``reason:
    "error.context.incomplete"``, the missing path in ``error.details`` —
    and the program MUST NOT build anyway." So the build must not have
    started: a silent fallback "would turn a forgotten file into an
    unauthenticated update path".
    """
    del build.files["keys/signing.pub"]
    (build.context / "keys" / "signing.pub").unlink()
    locked_context(build.context, build.files)

    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["status"] == "failure"
    assert document["reason"] == "error.context.incomplete"
    assert document["error"]["details"]["missing"] == ["keys/signing.pub"]
    assert build.plans == [] and build.children == []


def test_the_command_and_the_environment_a_build_would_run(build: BuildSetup) -> None:
    """Stage 5 assembles both, and §6.1 says what has to be in them.

    The command is :func:`~mcuhome.compiler.workspace.west_build_command`'s, so the
    per-image snippet rule and the board come out of the device model in
    the context rather than out of the request. The environment is built
    from nothing: ``ZEPHYR_BASE`` because "it finds Zephyr via
    ``find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})``",
    ``TMPDIR`` because "the program points its children's ``TMPDIR``"
    at ``tmp`` (§4), the codegen shim because CHIP v1.5.1.0 ships without
    the helper its codegen imports, and the two job caps because neither
    the CHIP GN sub-build nor a sysbuild image's inner ninja inherits the
    outer ``-j``.
    """
    assert build.run() == abi.EXIT_SUCCESS
    plan = build.plans[0]
    head = ["west", "build", "--board", BOARD, "--build-dir", str(plan.build_dir)]
    assert plan.command[:6] == head
    assert "--sysbuild" in plan.command
    assert "-Dapp_SNIPPET=debug-rtt;matter;boot-mode" in plan.command
    assert "-Dmcuboot_SNIPPET=boot-mode" in plan.command
    assert plan.topdir == build.workspace_dir
    assert plan.env["ZEPHYR_BASE"] == str(build.workspace_dir / "zephyr")
    assert plan.env["TMPDIR"] == str(build.tmp)
    assert plan.env["PYTHONPATH"] == str(build.sdk / "scripts" / "pyshim")
    assert plan.env["CCACHE_BASEDIR"] == str(build.workspace_dir)


def test_limits_jobs_is_authoritative_and_nothing_re_detects_it(
    build: BuildSetup, monkeypatch
) -> None:
    """§5.2: "It is not a hint … a foreign program would fall back to
    ``nproc``, which is exactly the case the field exists against."

    In the ``subprocess`` profile the program shares a host with other
    sessions, so ``nproc`` jobs each is an out-of-memory kill. The number
    reaches all three channels that do not inherit it, and
    :func:`~mcuhome.compiler.workspace.resolve_jobs` — the host-side resolver with
    the auto-detection in it — is made to explode so that calling it would
    fail this test rather than pass it quietly.
    """
    monkeypatch.setattr(
        abi.workspace, "resolve_jobs", lambda **kwargs: pytest.fail("jobs were re-detected")
    )
    assert build.run(limits={"jobs": 3}) == abi.EXIT_SUCCESS
    plan = build.plans[0]
    assert "-o=-j3" in plan.command
    assert plan.env[abi.workspace.CHIP_JOBS_VAR] == "3"
    assert plan.env[abi.workspace.CMAKE_JOBS_VAR] == "3"


@pytest.mark.parametrize(
    ("what", "params"),
    [("absent", None), ("without a mode", {"x-vendor": 1}), ("empty", {})],
)
def test_three_shapes_of_absent_params_all_mean_clean(
    build: BuildSetup, what: str, params: dict[str, Any] | None
) -> None:
    """§5.2: all three "are the same thing and all three mean
    ``mode: "clean"``".

    "A program MUST NOT treat a missing ``mode`` as 'whatever is
    cheapest', as 'repeat the last build's mode', or as an error." What
    ``clean`` costs is asserted rather than assumed: ``--pristine
    always``, which is the only spelling that does not depend on what the
    build directory happens to look like.
    """
    fields = {} if params is None else {"params": params}
    assert build.run(**fields) == abi.EXIT_SUCCESS, what
    command = build.plans[0].command
    assert command[command.index("--pristine") + 1] == "always"


def test_an_incremental_without_prior_state_of_this_session_is_executed_as_clean(
    build: BuildSetup,
) -> None:
    """§7.2 defines the fallback and §6.3 defines the predicate.

    "An ``incremental`` for which the program finds no prior state of
    *this session* in ``work`` is executed as ``clean``" — and "Absence of
    a marker means nothing. … It concludes only 'no prior state of this
    session'". A ``work`` holding somebody's leftovers but no marker of
    this session is exactly that case, so the leftovers are not built on.
    """
    (build.work / abi.WORK_BUILD).mkdir()
    (build.work / abi.WORK_BUILD / "CMakeCache.txt").write_text("stale\n", encoding="utf-8")

    assert build.run(params={"mode": "incremental"}) == abi.EXIT_SUCCESS
    command = build.plans[0].command
    assert command[command.index("--pristine") + 1] == "always"
    assert not (build.work / abi.WORK_BUILD / "CMakeCache.txt").exists()


def test_the_second_incremental_of_a_session_keeps_what_the_first_left(
    build: BuildSetup,
) -> None:
    """The other side of the same predicate: a marker this session wrote.

    ``work`` is "the session's persistent working area, exclusive to this
    session" (§5.2), and the marker §6.3 permits is what lets the program
    tell "mine" from "no idea". Only then does ``incremental`` mean
    anything at all, which is why this program writes one — see
    :mod:`mcuhome.compiler.abi`'s docstring.
    """
    assert build.run() == abi.EXIT_SUCCESS
    (build.work / abi.WORK_TREE / "keep-me").write_text("x\n", encoding="utf-8")

    assert build.run(params={"mode": "incremental"}) == abi.EXIT_SUCCESS
    command = build.plans[1].command
    assert command[command.index("--pristine") + 1] == "auto"
    assert (build.work / abi.WORK_TREE / "keep-me").is_file()


def test_a_mode_this_program_does_not_implement_is_refused_rather_than_downgraded(
    build: BuildSetup,
) -> None:
    """§5.2's warning, applied where ``required`` did not name the pointer.

    "A program that knows ``/params/mode`` but not the value
    ``reproducible`` MUST refuse … rather than accept the job and quietly
    deliver something else." §7.2 enumerates two values and says nothing
    about a third arriving unannounced; executing it as ``clean`` would be
    the same lie one channel down. See :mod:`mcuhome.compiler.abi`'s docstring.
    """
    assert build.run(params={"mode": "reproducible"}) == abi.EXIT_FAILURE
    document = build.document()
    assert document["status"] == "unsupported"
    assert document["reason"] == "unsupported.request"
    assert build.plans == []


def test_a_required_mode_this_program_cannot_honour_is_unsupported_required(
    build: BuildSetup,
) -> None:
    """§5.2's own example, and the reason rule 2 weighs values at all.

    "A program that knows ``/params/mode`` but not the value
    ``reproducible`` MUST refuse with ``unsupported.required`` rather than
    accept the job and quietly deliver something else." The pointer is one
    this program honours — it is in ``build``'s table and a ``clean`` there
    passes — so this is the value half of the rule and nothing else.
    """
    document = build.request(params={"mode": "reproducible"}, required=["/params/mode"])
    assert build.backend.run("build", document) == abi.EXIT_FAILURE
    answer = build.document()
    assert answer["status"] == "unsupported"
    assert answer["reason"] == "unsupported.required"
    assert answer["error"]["details"]["required"] == ["/params/mode"]
    assert build.plans == []


def test_build_honours_the_pointers_it_acts_on_and_not_the_ones_it_does_not(
    build: BuildSetup,
) -> None:
    """§5.2 rule 2, per action — the table is not one list.

    A ``build`` acts on ``/params/mode``, ``/out`` and ``/limits/jobs``,
    so it promises them. It does not implement cancellation, which §8
    makes a SHOULD "so that a fifty-line third-party program stays
    possible", so ``/cancel`` is not in its table and a backend demanding
    it is told which pointer failed instead of being served a build that
    would ignore the file.
    """
    honoured = ["/params/mode", "/out", "/work", "/tmp", "/limits/jobs", "/trees/sdk", "/context"]
    assert build.run(params={"mode": "clean"}, required=honoured) == abi.EXIT_SUCCESS

    assert build.run(cancel=str(build.tmp / "cancel"), required=["/cancel"]) == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "unsupported.required"
    assert document["error"]["details"]["required"] == ["/cancel"]


@pytest.mark.parametrize("field", ["session", "out", "work", "tmp", "context", "trees", "limits"])
def test_a_build_refuses_over_every_field_five_two_makes_mandatory(
    build: BuildSetup, field: str
) -> None:
    """Rule 3, and the mirror image of ``verify``'s two.

    §5.2 makes seven fields mandatory for every working action and §4.1
    forbids requiring what an action does not need — a ``build`` needs all
    seven and acts on all seven, so each one missing is a refusal a
    backend can read rather than a traceback ten minutes into a compile.
    """
    document = build.request()
    del document[field]
    assert build.backend.run("build", document) == abi.EXIT_FAILURE, field
    answer = build.document()
    assert answer["status"] == "unsupported"
    assert answer["reason"] == "unsupported.request"
    assert "context" not in answer


def test_a_work_directory_marked_for_another_session_is_terminal(build: BuildSetup) -> None:
    """§6.3: the one thing ``session`` is for beyond being echoed.

    "The program MUST fail it — ``status: "failure"``, ``reason:
    "error.work.foreign"``, the two session IDs in ``error.details`` — and
    it MUST NOT do any of the three things that would otherwise be
    tempting: it MUST NOT use the state it found, MUST NOT delete or
    overwrite it, and MUST NOT fall back to a private working area of its
    own choosing. It writes nothing into ``work`` in this case, not even
    its own marker."
    """
    marker = build.work / abi.WORK_MARKER
    marker.write_text(json.dumps({"session": "s-99"}), encoding="utf-8")

    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["status"] == "failure"
    assert document["reason"] == "error.work.foreign"
    assert document["error"]["details"] == {"session": "s-42", "found": "s-99"}
    assert json.loads(marker.read_text(encoding="utf-8")) == {"session": "s-99"}
    assert sorted(path.name for path in build.work.iterdir()) == [abi.WORK_MARKER]


def test_a_patched_layer_is_applied_once_per_session_and_reported(build: BuildSetup) -> None:
    """§6.2: applied "**once per session**", in §3.1's order, with ``-p1``.

    "The patch set of a locked context cannot change, so there is no
    comparison to perform and nothing to reapply." The second invocation
    of the same session therefore applies nothing and still reports the
    layer, because §5.4 makes ``layers`` mandatory "for every patched
    layer" of a successful build rather than for every layer this
    invocation happened to touch.
    """
    build.patch("zephyr", "0002-b.patch", "x\n")
    build.patch("zephyr", "0001-a.patch", "--- a\n+++ b\n")
    trees = build.trees(zephyr=True)

    assert build.run(trees=trees) == abi.EXIT_SUCCESS
    applied = [child for child in build.children if child["command"][0] == "git"]
    assert [Path(child["command"][-1]).name for child in applied] == [
        "0001-a.patch",
        "0002-b.patch",
    ]
    assert applied[0]["command"][:5] == [
        "git",
        "-C",
        str(build.layers["zephyr"]),
        "apply",
        "--verbose",
    ]
    assert "-p1" in applied[0]["command"]
    expected = {"zephyr": {"patchset": PATCHSET_OF_TWO}}
    assert build.document()["layers"] == expected

    build.children.clear()
    assert build.run(trees=trees) == abi.EXIT_SUCCESS
    assert [child for child in build.children if child["command"][0] == "git"] == []
    assert build.document()["layers"] == expected


def test_the_patchset_value_is_the_encoding_five_four_fixes(tmp_path: Path) -> None:
    """§5.4 states it as an algorithm "otherwise a cross-implementation
    audit is worthless".

    So this asserts against a literal computed once from the stated rule,
    not against a second run of the same code — the whole value of a fixed
    encoding is that a program in another language can be wrong against
    it. The empty case is here because the encoding still has its prefix
    line, which is the first thing an independent implementation drops.
    """
    layer = tmp_path / "zephyr"
    layer.mkdir()
    assert abi.patchset(layer) == (
        "sha256:1611cbd411fec49aabdba48f4fc72302646073d862bd32b3a1329e87d3f24748"
    )
    (layer / "0002-b.patch").write_text("x\n", encoding="utf-8")
    (layer / "0001-a.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
    assert abi.patchset(layer) == PATCHSET_OF_TWO


def test_a_layer_recorded_as_started_and_not_complete_is_terminal(build: BuildSetup) -> None:
    """§6.2: the interrupted patch application, which is not recoverable.

    "If the program finds a layer recorded as started but not complete, it
    MUST fail the invocation with ``reason: "error.patch.incomplete"`` …
    The client's remedy is a **new session** — a new container, hence
    pristine trees." Restoring the baseline from inside a merged view is
    not possible at all: "deleting a file there creates a whiteout instead
    of restoring the base".
    """
    build.patch("zephyr", "0001-a.patch", "--- a\n+++ b\n")
    records = build.work / abi.WORK_PATCH_RECORDS
    records.mkdir(parents=True)
    (records / "zephyr.json").write_text(
        json.dumps({"layer": "zephyr", "state": "started"}), encoding="utf-8"
    )

    assert build.run(trees=build.trees(zephyr=True)) == abi.EXIT_FAILURE
    document = build.document()
    assert document["status"] == "failure"
    assert document["reason"] == "error.patch.incomplete"
    assert document["error"]["details"]["layer"] == "zephyr"
    assert build.plans == []


def test_a_patch_layer_this_program_does_not_know_stops_the_invocation(
    build: BuildSetup,
) -> None:
    """§6.2: "the program MUST NOT proceed: ``status: "failure"``,
    ``reason: "error.layer.unknown"``".

    Building on regardless is the interesting failure this prevents: the
    patch would be silently unapplied, the build would succeed, and the
    artifact would be attributed to a context ID that promises the patch
    went in.
    """
    build.patch("x-vendor", "0001-a.patch", "--- a\n+++ b\n")
    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.layer.unknown"
    assert document["error"]["details"]["layer"] == "x-vendor"
    assert build.plans == []


def test_a_known_layer_patched_without_a_trees_entry_is_the_same_refusal(
    build: BuildSetup,
) -> None:
    """§6.2's *other* condition, same reason: "a layer for which there is
    no ``trees`` entry, **or** which the program does not know".

    One reason, two conditions, and the first is the subtler one: the
    layer is perfectly known, the backend simply supplied no writable
    view to apply the patch into (§4.1 requires one "for **every** layer
    that carries patches"). A first implementation answered this half
    with ``error.build.failed`` and only review caught it — a backend
    matching on ``reason`` to supply the missing entry would never have
    seen the case.
    """
    build.patch("zephyr", "0001-a.patch", "--- a\n+++ b\n")
    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.layer.unknown"
    assert document["error"]["details"]["layer"] == "zephyr"
    assert build.plans == []


def test_a_tree_this_program_cannot_move_its_workspace_to_is_refused(
    build: BuildSetup,
) -> None:
    """The one piece §6.1 assigns and names no mechanism for.

    West resolves project paths from ``.west/config`` plus the manifest,
    and nothing in the contract or in this repository moves them at
    invocation time. So a ``trees`` entry naming somewhere other than
    where this image keeps the layer is ``error.build.failed`` — never a
    build against a tree the backend did not name. See
    :mod:`mcuhome.compiler.abi`'s docstring for what would replace it.
    """
    trees = build.trees()
    trees["zephyr"] = {"path": str(build.root / "elsewhere" / "zephyr"), "writable": True}
    assert build.run(trees=trees) == abi.EXIT_FAILURE
    document = build.document()
    assert document["status"] == "failure"
    assert document["reason"] == "error.build.failed"
    assert str(build.layers["zephyr"]) in document["error"]["message"]
    assert build.plans == []


def test_the_generate_child_is_invoked_over_this_same_abi(build: BuildSetup) -> None:
    """§6.1: "``<trees.sdk.path>/<generate.program> generate <absolute path
    of a request document>`` … That is §5.1 unchanged".

    "The program writes that request document into its own ``tmp`` and is
    the *backend* of that invocation" — and a backend owes every
    invocation what §4's table promises, so the ``out`` the child
    receives is a **fresh, empty** per-invocation directory, not the
    session's tree. A foreign entry point (E30: it need not be MCUHome's)
    may rely on that emptiness; one that listed ``out`` before writing
    would otherwise see another invocation's files. What the child
    produced is then absorbed into ``work/tree`` content-aware, which is
    where the compile reads it.
    """
    assert build.run() == abi.EXIT_SUCCESS
    child = next(one for one in build.children if one["command"][1] == "generate")
    assert child["command"][0] == str(build.sdk / "bin" / "generate")
    request_path = Path(child["command"][2])
    assert request_path.is_absolute()
    assert build.tmp in request_path.parents
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["request"] == 1
    assert request["context"] == str(build.context)
    child_out = Path(request["out"])
    assert child_out != build.work / abi.WORK_TREE
    assert build.tmp in child_out.parents, "per invocation, so it starts empty (§4)"
    assert child["out_entries"] == [], "the child received the emptiness §4 promises"
    assert Path(request["result"]).is_absolute()
    assert (build.work / abi.WORK_TREE / "app" / "CMakeLists.txt").is_file()


@pytest.mark.parametrize(
    ("what", "arrange"),
    [
        ("no mcuhome-sdk.json at all", "remove"),
        ("an sdk metadata version nobody implements", "version"),
        ("a missing generate.runtime", "field"),
        ("a generate.program that escapes the tree", "escape"),
    ],
)
def test_code_generation_that_cannot_be_reached_is_error_build_failed(
    build: BuildSetup, what: str, arrange: str
) -> None:
    """§6.1: "all three fail the invocation with ``reason:
    "error.build.failed"``. They are not ``unsupported``".

    "The program implements everything this contract asks of it, and no
    other container would fare better with this SDK package" — so a
    backend that answered ``unsupported`` here would be told to go and
    find another image for a package no image can read.
    """
    if arrange == "remove":
        (build.sdk / abi.SDK_METADATA_FILE).unlink()
    elif arrange == "version":
        build.write_sdk_metadata({**SDK_METADATA, "sdk": 2})
    elif arrange == "field":
        build.write_sdk_metadata({"sdk": 1, "generate": {"program": "bin/generate"}})
    else:
        build.write_sdk_metadata(
            {"sdk": 1, "generate": {"program": "../../escape", "runtime": "py"}}
        )

    assert build.run() == abi.EXIT_FAILURE, what
    document = build.document()
    assert document["status"] == "failure", what
    assert document["reason"] == "error.build.failed", what
    assert build.plans == []


def test_a_build_that_does_not_compile_declares_nothing(build: BuildSetup) -> None:
    """§5.4: ``error.build.failed`` "covers every failure of the build
    itself … a compiler diagnostic is not a classification a frozen
    registry can enumerate, it is text".

    And the "on success" rows stay away: "a ``build`` refused with
    ``unsupported.required`` has no artifacts. … Fabricating either would
    be worse than omitting it". The measured context ID is reported, since
    that one *was* measured.
    """
    build.build_code = 1
    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.build.failed"
    assert "artifacts" not in document and "layers" not in document
    assert document["context"] == build.manifest.id
    assert list(build.out.iterdir()) == []


def test_a_patch_that_does_not_apply_stops_the_invocation(build: BuildSetup) -> None:
    """§6.2: "a patch that does not apply is a failure of the invocation
    and not something to search around" — no ``--3way``, no fallback.

    The child's own words travel in ``error.message``, because "a
    compiler diagnostic is not a classification a frozen registry can
    enumerate, it is text" — and ``git apply --verbose``'s reject output
    is the only thing that tells a user *which hunk*.
    """
    build.patch("zephyr", "0001-does-not-apply.patch", "--- a\n+++ b\n")
    build.child_code = 1
    assert build.run(trees=build.trees(zephyr=True)) == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.build.failed"
    assert "0001-does-not-apply.patch" in document["error"]["message"]
    assert build.plans == [], "nothing compiled after a failed patch"


def test_a_generate_child_that_dies_is_error_build_failed(build: BuildSetup) -> None:
    """§6.1: "A non-zero exit … fails the invocation with ``reason:
    "error.build.failed"``."

    The child's exit code and its last words are in the message — the
    program is the backend of that invocation, and a backend that
    swallowed them would leave the user with "generation failed" and
    nothing to act on.
    """
    build.child_code = 1
    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.build.failed"
    assert "exited 1" in document["error"]["message"]


def test_a_generate_child_that_writes_no_result_document_is_a_failure(
    build: BuildSetup,
) -> None:
    """§6.1: "a missing result document … fails the invocation".

    Exit 0 with no document is the shape of a child that was killed
    after its work but before its last write — believing the exit code
    alone would attribute a half-written tree to a successful step.
    """
    build.generate_writes_result = False
    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.build.failed"
    assert "no readable result document" in document["error"]["message"]


def test_a_generate_child_that_answers_failure_is_believed(build: BuildSetup) -> None:
    """§6.1: "a ``status`` other than ``success`` fails the invocation".

    The child said failure with exit 0 — the document wins, exactly as
    §5.3 has the backend read the program's own results: "Where exit
    code and document contradict each other, the pessimistic reading
    wins."
    """
    build.generate_status = "failure"
    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.build.failed"
    assert "did not succeed" in document["error"]["message"]
    assert build.plans == [], "nothing compiled on a refused generation"


def test_a_compiler_that_cannot_start_is_a_typed_failure(build: BuildSetup) -> None:
    """`run_build` raising is the compiler never starting — still exit 1.

    §5.3's exit 1 "promises a result document", so the one failure mode
    that must not exist is a raise that escapes as a traceback: the
    backend would read "the program died. Undefined forever." for a
    condition the program had fully diagnosed.
    """
    build.build_raises = "the compiler could not even start"
    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.build.failed"
    assert "could not even start" in document["error"]["message"]


def test_a_build_that_leaves_no_application_image_is_a_failure(build: BuildSetup) -> None:
    """§7.2's floor is "at least two artifacts", and one of them is gone.

    A zero-exit ``west build`` without a ``zephyr.hex`` is a build system
    lying about its own success; declaring the remaining files anyway
    would hand the backend an artifact set that flashes nothing.
    """
    build.omit_app_hex = True
    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.build.failed"
    assert "zephyr.hex" in document["error"]["message"]
    assert "artifacts" not in document


def test_a_build_that_leaves_no_kconfig_cannot_state_the_signing_block(
    build: BuildSetup,
) -> None:
    """§7.2.1 makes ``signing`` mandatory, and its ``version`` lives in
    the built application's own ``.config``.

    "A build that left no ``.config`` behind has no version to state" —
    and a report without the block would let a client sign with a guessed
    version, which MCUboot then compares monotonically against reality.
    """
    build.omit_config = True
    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.build.failed"
    assert ".config" in document["error"]["message"]


def test_a_board_without_an_update_scheme_cannot_be_built(build: BuildSetup) -> None:
    """The registry is what knows the MCUboot layout (ADR 0015 decision 2).

    A model naming a board this builder has no update scheme for cannot
    produce the ``signing`` block §7.2.1 makes mandatory, so the refusal
    comes before the compile — typed, with the board named — rather than
    as an unusable report after fifteen minutes of runner time.
    """
    build.files["model/device-model.json"] = build.files["model/device-model.json"].replace(
        "nrf7002dk/nrf5340/cpuapp", "vendor/board-nobody-knows"
    )
    build.manifest = locked_context(build.context, build.files)
    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.build.failed"
    assert "vendor/board-nobody-knows" in document["error"]["message"]
    assert build.plans == [], "the refusal comes before the compile"


def test_the_reported_context_is_the_one_the_program_measured(build: BuildSetup) -> None:
    """§5.4: "computed by the program from the context as materialized. It
    exists **for comparison only**".

    A tampered context still builds and still reports honestly — §7.2
    never makes the disagreement a build failure, and §7.3 is the action
    that exists to find it. What must not happen is the declared ID coming
    back: the backend compares ``result.context`` against its own
    measurement (§9.3), and an echoed value would agree with everything.
    """
    (build.context / "model" / "device-model.json").write_text(
        build.files["model/device-model.json"].replace("bench-node", "bench-nodf", 1),
        encoding="utf-8",
    )
    assert build.run() == abi.EXIT_SUCCESS
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", build.document()["context"])
    assert build.document()["context"] != build.manifest.id


def test_a_build_writes_nothing_into_the_context(build: BuildSetup) -> None:
    """§3.1: "Build outputs MUST NOT be written into the context. The
    context is a read-only input for the whole life of a session."

    §9.2 point 1 lists the write scope and ends "``context`` is not among
    them". Everything this build produced went to ``out``, ``work`` and
    ``tmp``, all three of which the request named.
    """
    before = sorted(path.relative_to(build.context).as_posix() for path in build.context.rglob("*"))
    assert build.run() == abi.EXIT_SUCCESS
    after = sorted(path.relative_to(build.context).as_posix() for path in build.context.rglob("*"))
    assert after == before


def test_a_read_only_shared_cache_is_a_secondary_one(build: BuildSetup) -> None:
    """§10: "the program MUST treat it as a read-only secondary cache,
    with its own primary cache in ``work`` or ``tmp``".

    "Jobs benefit from the warm shared cache and cache within their own
    build without ever writing the shared store." ``writable`` is read
    from the request and never probed (§4.1) — a program cannot tell a
    read-only bind mount from a full disk by trying.
    """
    shared = build.root / "shared-ccache"
    shared.mkdir()
    assert build.run(ccache={"path": str(shared), "writable": False}) == abi.EXIT_SUCCESS
    env = build.plans[0].env
    assert env["CCACHE_DIR"] == str(build.work / abi.WORK_CCACHE)
    assert env["CCACHE_REMOTE_STORAGE"] == f"file:{shared}|read-only"
    assert list(shared.iterdir()) == []


def test_a_writable_shared_cache_may_be_the_primary_one(build: BuildSetup) -> None:
    """§10's other half: "the program MAY use it as its primary cache"."""
    shared = build.root / "shared-ccache"
    shared.mkdir()
    assert build.run(ccache={"path": str(shared), "writable": True}) == abi.EXIT_SUCCESS
    env = build.plans[0].env
    assert env["CCACHE_DIR"] == str(shared)
    assert "CCACHE_REMOTE_STORAGE" not in env


def test_events_are_ndjson_with_a_monotonic_seq(build: BuildSetup) -> None:
    """§8: "one JSON object per line, UTF-8, flushed after every line,
    append-only … a monotonic ``seq`` starting at 1".

    The names are the registry's and they mean what §8 says they mean:
    ``invocation.started`` "as soon as the request document is understood
    and before any work", ``context.checked`` "after the effective context
    ID has been computed", ``artifact.collected`` "after it exists on
    disk", ``invocation.finished`` "immediately before the result document
    is written". Offering fewer names than the table is conforming, so
    this asserts the ones emitted rather than the whole registry.
    """
    events = build.tmp / "events.ndjson"
    assert build.run(events=str(events)) == abi.EXIT_SUCCESS
    lines = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert [line["seq"] for line in lines] == list(range(1, len(lines) + 1))
    assert lines[0] == {"event": "invocation.started", "seq": 1, "action": "build"}
    assert lines[-1]["event"] == "invocation.finished"
    assert lines[-1]["status"] == "success"
    names = [line["event"] for line in lines]
    assert names.count("context.checked") == 1
    assert names.count("artifact.collected") == 4
    checked = next(line for line in lines if line["event"] == "context.checked")
    assert checked["context"] == build.document()["context"]
    region = next(line for line in lines if line["event"] == "build.memory.region")
    assert set(region) == {"event", "seq", "image", "region", "used", "total", "percent"}


def test_a_request_without_an_events_file_gets_no_events(build: BuildSetup) -> None:
    """§8: "Only if the request document carries ``events``."

    "a backend that offers no ``events`` file gets none" — and a build
    that quietly created one would be writing outside the scope §9.2 point
    1 fixes, which names ``events`` as one of exactly three files the
    request document has to point at before the program may touch it.
    """
    assert build.run() == abi.EXIT_SUCCESS
    assert sorted(path.name for path in build.out.iterdir()) == [
        "bootloader.hex",
        "build-report.json",
        "firmware.bin",
        "firmware.hex",
    ]
    assert list(build.root.rglob("*.ndjson")) == []


def test_a_program_with_no_workspace_of_its_own_says_so(build: BuildSetup) -> None:
    """§6.1: "The backend never supplies a workspace."

    A program that carries none cannot assemble a build environment out of
    ``context``, ``trees`` and ``work``, and the ``describe`` it would
    have given says as much — every layer ``"path": null``, which asks the
    backend for trees this program then could not build against. Failing
    is the honest end of that; compiling against whatever the image
    happens to contain is not.
    """
    build.backend.record.unlink()
    assert build.run() == abi.EXIT_FAILURE
    document = build.document()
    assert document["reason"] == "error.build.failed"
    assert build.plans == []


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
    layer names, so this is a lookup and not a translation.

    The ``mounted`` layer is reported **at the path the record names**,
    with no version: the SDK is a hash-pinned package fetched per session
    (ADR 0018), but west resolves project paths from ``.west/config``
    plus the manifest, so this image needs it mounted at exactly that
    directory — and ``build`` refuses any other. Reporting ``null`` here
    ("put it wherever you like", §7.1.1) once made ``describe`` and
    ``build`` contradict each other: a backend arranged by our own
    ``describe`` could never have built. ``null`` remains for the case it
    is true of — a layer the record does not place, like ``mcuboot``
    below. §4 sanctions the requirement since the D1 erratum — "A
    ``trees`` entry is the one thing a program may have a fixed path
    for" — and this test pins both halves: the declaration is truthful,
    and the two actions tell one story.
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
        "sdk": {"path": "/mcuhome/workspace/mcuhome"},
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

    The ABI lives in :mod:`mcuhome.compiler.abi` because the ``subprocess`` profile
    runs the same code with no image and no launcher around it (§1.2). If
    this file ever grows a second job, the two profiles stop being one
    implementation.
    """
    text = LAUNCHER.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "exec" in text and "mcuhome.compiler.abi" in text
    assert LAUNCHER.suffix == "", "§2.2: no extension — a third party may ship a binary here"
    assert os.access(LAUNCHER, os.X_OK), "the file in the repository is executable"


# --------------------------------------------------------------------------
# The SDK package's own entry point (§6.1)
# --------------------------------------------------------------------------

SDK_ROOT = Path(__file__).resolve().parent.parent


def test_the_sdk_package_declares_its_entry_point() -> None:
    """§6.1, held against the real repository rather than a fixture.

    "At the root of the tree the backend hands over as ``trees.sdk``
    there is a file named ``mcuhome-sdk.json``" — and for MCUHome's own
    SDK that tree is this repository. Every ``build`` test in this file
    manufactures the metadata in its fixture, so without this test the
    real package could ship without the file (it did — the review that
    forced this test found exactly that) and nothing would go red.

    Parsed through :func:`abi.sdk_entry_point` — the same rules the
    program applies to a foreign SDK — so the fixture's schema and the
    real file cannot drift apart without one of them failing.
    """
    entry, runtime = abi.sdk_entry_point(SDK_ROOT)
    assert entry == SDK_ROOT / "bin" / "generate"
    assert entry.is_file()
    assert os.access(entry, os.X_OK), "the entry point is invoked, not imported (§6.1)"
    assert runtime == "python3"


def test_the_entry_point_generates_the_real_tree(tmp_path, device_model_json: str) -> None:
    """The real stage 4, reached the way a build container reaches it.

    In-process through :func:`mcuhome.compiler.sdkentry.main` — the launcher adds
    nothing but ``sys.path`` — with a real context directory and a real
    resolved model. The tree it writes is compared against
    :func:`mcuhome.compiler.generate.generate` for the same model, which is the
    byte-identity the module docstring promises: a remote build's
    application tree is not a second code path.
    """
    from mcuhome.compiler import sdkentry
    from mcuhome.compiler.generate import generate
    from mcuhome.workbench.api import read_model

    context = tmp_path / "ctx"
    (context / "model").mkdir(parents=True)
    (context / "model" / "device-model.json").write_text(device_model_json, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    result = tmp_path / "result.json"
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "request": 1,
                "result": str(result),
                "session": "s-42",
                "context": str(context),
                "out": str(out),
            }
        ),
        encoding="utf-8",
    )

    assert sdkentry.main(["generate", "generate", str(request)]) == abi.EXIT_SUCCESS
    answer = json.loads(result.read_text(encoding="utf-8"))
    assert answer["status"] == "success"
    assert answer["action"] == "generate"
    assert answer["session"] == "s-42"

    model = read_model(context / "model" / "device-model.json")
    expected = generate(model, config_name=model.device.source)
    for relative, content in expected.items():
        assert (out / relative).read_text(encoding="utf-8") == content, relative


def test_the_entry_point_refuses_what_it_cannot_generate(tmp_path) -> None:
    """A context without a model is a ``failure`` the caller can read.

    §6.1 maps any non-``success`` onto ``error.build.failed``; answering
    in that vocabulary means the message a user finally sees carries the
    actual cause instead of a generic wrapper around a lost detail.
    """
    from mcuhome.compiler import sdkentry

    context = tmp_path / "ctx"
    context.mkdir()
    result = tmp_path / "result.json"
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "request": 1,
                "result": str(result),
                "context": str(context),
                "out": str(tmp_path / "out"),
            }
        ),
        encoding="utf-8",
    )
    assert sdkentry.main(["generate", "generate", str(request)]) == abi.EXIT_FAILURE
    answer = json.loads(result.read_text(encoding="utf-8"))
    assert answer["status"] == "failure"
    assert answer["reason"] == "error.build.failed"
    assert "device-model.json" in answer["error"]["message"]


def test_the_launcher_runs_the_real_entry_point(tmp_path, device_model_json: str) -> None:
    """``bin/generate`` end to end, as a child process, shebang included.

    One subprocess in the whole suite, deliberately: the in-process tests
    above prove the action, and this proves the one thing they cannot —
    that the file §6.1 names is startable by a caller that knows nothing
    but the path and the ABI. A backend written in Go gets exactly this.
    """
    import subprocess
    import sys

    context = tmp_path / "ctx"
    (context / "model").mkdir(parents=True)
    (context / "model" / "device-model.json").write_text(device_model_json, encoding="utf-8")
    out = tmp_path / "out"
    result = tmp_path / "result.json"
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"request": 1, "result": str(result), "context": str(context), "out": str(out)}),
        encoding="utf-8",
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(SDK_ROOT / "bin" / "generate"), "generate", str(request)],
        capture_output=True,
        text=True,
        env={"PATH": str(Path(sys.executable).parent)},
    )
    assert completed.returncode == abi.EXIT_SUCCESS, completed.stderr
    assert json.loads(result.read_text(encoding="utf-8"))["status"] == "success"
    assert (out / "app" / "CMakeLists.txt").is_file()


def test_a_crash_inside_an_action_still_writes_a_result_document(backend: Backend) -> None:
    """§5.3: exit 1 "promises a result document" — a traceback is neither.

    Driven through :func:`abi.run_invocation` with an invoke that raises,
    which is exactly what an unhandled ``OSError`` in the middle of a
    ``build`` is. The alternative this pins against: the exception
    escapes, Python exits 1 with a traceback on stderr, and the backend
    reads a promised-but-absent result document — "the pessimistic
    reading wins **and** a contract violation is raised against the
    image" for a condition the program could have answered.
    """
    backend.request.write_text(json.dumps(backend.preamble(session="s-42")), encoding="utf-8")

    def explode(action: str, document: dict[str, Any]) -> dict[str, Any]:
        raise OSError("disk went away mid-action")

    code = abi.run_invocation([PROGRAM_PATH, "build", str(backend.request)], explode)
    assert code == abi.EXIT_FAILURE
    document = backend.document()
    assert document["status"] == "failure"
    assert document["action"] == "build"
    assert document["session"] == "s-42"
    assert document["reason"] == "error.build.failed"
    assert "disk went away mid-action" in document["error"]["message"]
