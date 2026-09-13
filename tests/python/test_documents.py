# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The documents against their reference: ``to_dict()`` versus ``docs/api.md``.

A client renders whole documents this package produced; it never
assembles one out of fields it read off an object. That only works while
the documents are what the reference says they are — every declared key
present, JSON-ready data inside, and the verdict first. Those three
promises are cheap to break by accident (a key added to a class and not
to its document, a ``Path`` left where a string belongs, a value that
silently disappears when it is ``None``) and nothing else fails when
they are.

So this module reads the reference's ``## Documents`` section and holds
every class named there against it.

**What is parsed, and how.** In that section a document is declared in
one of two forms, and a later reader keeps the section parseable by
staying with them:

* ``\\`Class.to_dict()\\``` followed by a fenced ``json`` block — the
  example document. Its top-level keys are the declared key set.
* ``\\`Class.to_dict()\\`: \\`{a, b, c}\\``` — the key set inline. Nested
  shapes inside the braces belong to the value they follow (``mismatches:
  [{path, ...}]``), so only the top level counts.

**What is not held against it.** A name the reference documents that the
package does not carry yet is listed in
:data:`~test_surface.PENDING` — the feature that brings it brings its
document. :data:`SAMPLES` is the other half: one instance per class,
built here, because a document can only be checked by asking a real
object for it. A documented class that is neither pending nor in
``SAMPLES`` fails, so the sample table cannot fall behind the reference.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT
from mcuhome.model.artifacts import Artifact
from mcuhome.model.context import (
    ContextFile,
    ContextManifest,
    DeveloperEnvironment,
    SdkPin,
)
from mcuhome.model.errors import ConfigError, Location
from mcuhome.model.pairing import TEST_PAIRING
from test_surface import PENDING

from mcuhome.workbench import api

REFERENCE = REPO_ROOT / "docs" / "api.md"

#: The options every configuration resolution declares, resolved once —
#: ``Settings`` is the one document whose keys are data rather than a
#: fixed list, and this is what it is keyed by.
SETTINGS = api.resolve_settings(project=None, env={})

ROOT = Path("/projects/attic")


def _sample_project() -> api.Project:
    return api.Project(root=ROOT, discovered=True)


def _sample_artifact() -> Artifact:
    return Artifact(root="out", path="firmware.bin", role="firmware", sha256="a" * 64)


def _sample_manifest() -> ContextManifest:
    return ContextManifest(
        sdk=SdkPin(constraint="", version="", url="", sha256=""),
        build_environment=DeveloperEnvironment(),
        board="nrf7002dk/nrf5340/cpuapp",
        files=(ContextFile(path="model.json", sha256="b" * 64),),
        id="sha256:" + "c" * 64,
    )


#: One instance per documented class, each carrying values in every key
#: it declares — a sample whose optional fields were all left out would
#: prove nothing about a document that must carry them anyway.
SAMPLES: dict[str, Callable[[], Any]] = {
    "Artifact": _sample_artifact,
    "Builder": lambda: api.Builder(
        name="attic",
        target="remote",
        origin="project",
        source=str(ROOT / "mcuhome.yaml"),
        server="10.0.0.5:8291",
    ),
    "BuildOptions": lambda: api.resolve_build_options(SETTINGS),
    "BuildResult": lambda: api.BuildResult(
        ok=True,
        target="local",
        device="thermostat",
        context_id="sha256:" + "c" * 64,
        artifacts=(_sample_artifact(),),
        out_dir=ROOT / "build" / "thermostat",
        report="build-report.json",
        container_image="ghcr.io/mcu-home/build-environment@sha256:" + "d" * 64,
        detail=object(),
    ),
    "ContextVerification": lambda: api.ContextVerification(
        root=ROOT / "build" / "thermostat" / "context",
        manifest=_sample_manifest(),
        actual_id="sha256:" + "e" * 64,
        mismatches=(
            api.FileMismatch(path="model.json", declared_sha256="b" * 64, actual_sha256=None),
        ),
    ),
    "Diagnostic": lambda: api.Diagnostic.warning(
        "secrets/main.yaml is readable by other users",
        kind="exposed_secret_file",
        location=Location(file=ROOT / "secrets" / "main.yaml", line=1, column=1, key="device.name"),
        hint="chmod 600 secrets/main.yaml",
    ),
    "FileMismatch": lambda: api.FileMismatch(
        path="model.json", declared_sha256="b" * 64, actual_sha256="f" * 64
    ),
    "Migration": lambda: api.plan_upgrade(0)[0],
    "NewDevice": lambda: api.NewDevice(
        project=_sample_project(),
        entry=ROOT / "devices" / "thermostat" / "main.yaml",
        name="thermostat",
        board="nrf7002dk/nrf5340/cpuapp",
    ),
    "NewPairing": lambda: api.NewPairing(
        entry=ROOT / "devices" / "thermostat" / "main.yaml",
        secrets_file=ROOT / "secrets" / "devices" / "thermostat.yaml",
        pairing=TEST_PAIRING,
        replaced=True,
    ),
    "NewProject": lambda: api.NewProject(
        project=_sample_project(), created=(ROOT / ".mcuhome-project-root",)
    ),
    "Project": _sample_project,
    "RegistrySettings": lambda: api.RegistrySettings(
        base_domain="packages.mcuhome.org",
        untrusted=False,
        mirrors={"sdk": ("https://mirror.example/sdk/",)},
        anchor=ROOT / "secrets" / "trust-anchor" / "packages.mcuhome.org.json",
    ),
    "RunningBuild": lambda: api.RunningBuild(
        directory=ROOT / "build" / "thermostat",
        device="thermostat",
        operation="build",
        process="4711",
        started="2026-09-13 10:00:00",
    ),
    "SelectedBuilder": lambda: api.SelectedBuilder(
        target="remote",
        builder=SAMPLES["Builder"](),
        server="10.0.0.5:8291",
        token="s3cret",
        container_image=None,
    ),
    "Setting": lambda: SETTINGS.setting("build.mode"),
    "Settings": lambda: SETTINGS,
    "StepResult": lambda: api.StepResult(
        action="build",
        context_id="sha256:" + "c" * 64,
        exit_code=0,
        result={"status": "success"},
        status="success",
        problems=("one thing",),
        violation=None,
        artifacts=(_sample_artifact(),),
        out_dir=ROOT / "build" / "thermostat" / "out",
    ),
    "StoreEntry": lambda: api.StoreEntry(
        kind="sdk",
        name="mcuhome-sdk-linux-x86_64",
        version="0.1.9",
        sha256="a" * 64,
        path=ROOT / ".local" / "sdk",
    ),
    "UpgradeRecord": lambda: api.UpgradeRecord(
        started=datetime.now(UTC).isoformat(timespec="seconds"),
        process=4711,
        host="bench",
        running="v1_project_identity",
    ),
    "UpgradeResult": lambda: api.UpgradeResult(
        from_version=1, to_version=2, applied=api.plan_upgrade(0), stopped=True
    ),
    "ValidationResult": lambda: api.ValidationResult(
        entry=ROOT / "devices" / "thermostat" / "main.yaml",
        project=_sample_project(),
        model=None,
        errors=(ConfigError("no such board", location=Location(file=ROOT / "x.yaml", line=2)),),
        warnings=(SAMPLES["Diagnostic"](),),
    ),
}


def _documents_section(text: str) -> str:
    """The reference's ``## Documents`` section, up to the next one."""
    section = text.split("\n## Documents\n", 1)[1]
    return section.split("\n## ", 1)[0]


def _top_level_keys(braces: str) -> list[str]:
    """The keys of one inline ``{a, b: [{c}], d}`` shape, outermost only."""
    keys: list[str] = []
    depth = 0
    current = ""
    for character in braces[1:-1]:
        if character in "[{(":
            depth += 1
        elif character in "]})":
            depth -= 1
        if character == "," and depth == 0:
            keys.append(current)
            current = ""
        else:
            current += character
    keys.append(current)
    return [key.split(":", 1)[0].strip().strip("`") for key in keys if key.strip()]


def _declared_documents(text: str) -> dict[str, list[str]]:
    """Every class the Documents section declares, with its key set."""
    section = _documents_section(text)
    declarations = list(re.finditer(r"`(\w+)\.to_dict\(\)`", section))
    names = [declaration.group(1) for declaration in declarations]
    assert len(names) == len(set(names)), (
        f"declared twice in the Documents section: "
        f"{sorted({name for name in names if names.count(name) > 1})}"
    )
    documents: dict[str, list[str]] = {}
    for index, declaration in enumerate(declarations):
        end = declarations[index + 1].start() if index + 1 < len(declarations) else len(section)
        rest = section[declaration.end() : end]
        block = re.search(r"```json\n(.*?)\n```", rest, re.DOTALL)
        if block is not None:
            documents[declaration.group(1)] = list(json.loads(block.group(1)))
            continue
        inline = re.search(r"`(\{.*?\})`", rest, re.DOTALL)
        assert inline is not None, f"{declaration.group(1)}: no example and no key set"
        documents[declaration.group(1)] = _top_level_keys(inline.group(1).replace("\n", " "))
    return documents


def _example(text: str, name: str) -> Any:
    """The example document the reference shows for *name*."""
    rest = _documents_section(text).split(f"`{name}.to_dict()`", 1)[1]
    block = re.search(r"```json\n(.*?)\n```", rest, re.DOTALL)
    assert block is not None, f"{name}: no example document"
    return json.loads(block.group(1))


DOCUMENTS = _declared_documents(REFERENCE.read_text("utf-8"))

#: ``Settings`` is keyed by option name rather than by a fixed key set,
#: so its example states one entry and is checked by its own test below.
KEYED_BY_DATA = ("Settings",)

#: Documents the reference states for a class that **exists** and does
#: not answer one yet, by the feature that brings it. The counterpart of
#: :data:`~test_surface.PENDING`, which covers names that do not exist
#: at all: a class cannot be pending once it is exported, and a document
#: it does not answer would otherwise go unnoticed.
#: :func:`test_an_owed_document_is_still_owed` empties this list from
#: the other side — the day the method arrives, the entry has to go.
OWED = {"SignPlan": "signing"}

CHECKED = sorted(
    name
    for name in DOCUMENTS
    if name not in PENDING and name not in OWED and name not in KEYED_BY_DATA
)


def _every_value(data: Any) -> list[Any]:
    """Every value inside a document, nested ones included."""
    if isinstance(data, dict):
        return [data, *[value for item in data.values() for value in _every_value(item)]]
    if isinstance(data, list):
        return [data, *[value for item in data for value in _every_value(item)]]
    return [data]


def test_the_reference_declares_the_documents_this_test_knows() -> None:
    """The sample table cannot fall behind the reference.

    A class documented as answering a document, not pending and without
    a sample here, would simply not be checked — and nothing would say
    so. This is what says so.
    """
    assert len(DOCUMENTS) > 15  # noqa: PLR2004 - a floor, so a broken parse cannot pass
    unsampled = sorted(name for name in CHECKED if name not in SAMPLES)
    assert not unsampled, f"{unsampled} are documented documents with no sample in this test"


def test_every_sample_is_a_documented_document() -> None:
    """And the other way round: no sample for something nobody documents."""
    undocumented = sorted(name for name in SAMPLES if name not in DOCUMENTS)
    assert not undocumented, f"{undocumented} have a sample here and no key set in the reference"


@pytest.mark.parametrize("name", CHECKED)
def test_the_document_carries_exactly_the_documented_keys(name: str) -> None:
    """Every declared key, and no key the reference does not declare.

    Both directions are the promise: a client reads a key without asking
    whether this version has it, and a key nobody documented is a
    surface that was never agreed on — the first client to find it makes
    it one.
    """
    document = SAMPLES[name]().to_dict()
    assert sorted(document) == sorted(DOCUMENTS[name]), (
        f"{name}: the reference declares {sorted(DOCUMENTS[name])}, "
        f"the code answers {sorted(document)}"
    )


@pytest.mark.parametrize("name", CHECKED)
def test_the_document_is_json_ready(name: str) -> None:
    """It survives ``json.dumps`` — which is all a document is for."""
    assert json.dumps(SAMPLES[name]().to_dict())


@pytest.mark.parametrize("name", CHECKED)
def test_no_python_object_reaches_a_document(name: str) -> None:
    """No ``Path``, no tuple, no dataclass — at any depth.

    ``json.dumps`` would catch a ``Path`` and a dataclass; it would not
    catch a tuple, which serializes as a list and looks right until
    somebody compares two documents. And a value that is JSON-ready by
    accident (a dataclass with a ``__str__``) is caught here rather than
    in a client.
    """
    for value in _every_value(SAMPLES[name]().to_dict()):
        assert not isinstance(value, Path | tuple), f"{name}: {value!r}"
        assert not dataclasses.is_dataclass(value), f"{name}: {value!r}"


@pytest.mark.parametrize("name", CHECKED)
def test_a_verdict_is_the_first_key(name: str) -> None:
    """Where a document has an ``ok``, it is the first thing in it."""
    document = SAMPLES[name]().to_dict()
    if "ok" in document:
        assert next(iter(document)) == "ok", f"{name}: ok is not the first key"


def test_the_pairing_sub_document_carries_what_the_reference_declares() -> None:
    """The one nested shape the reference spells out key by key.

    :func:`_top_level_keys` reads the outermost braces, which is right
    for every other document here — the nested ones are documents of
    their own and checked as such. ``NewPairing`` is the exception: the
    credentials it carries come from a value that has no ``to_dict()``
    of its own (the model package's ``Pairing``), so the keys are
    written out in this package and would otherwise be checked by
    nothing.
    """
    section = _documents_section(REFERENCE.read_text("utf-8"))
    paragraph = section.split("`NewPairing.to_dict()`", 1)[1]
    shapes = re.findall(r"`(\{.*?\})`", paragraph, re.DOTALL)
    # The first shape is the document itself, the second the `pairing`
    # value inside it.
    declared = _top_level_keys(shapes[1].replace("\n", " "))

    document = SAMPLES["NewPairing"]().to_dict()["pairing"]
    assert sorted(document) == sorted(declared), (
        f"the reference declares {sorted(declared)} inside `pairing`, "
        f"the code answers {sorted(document)}"
    )
    assert json.dumps(document)


def test_the_settings_document_has_one_entry_per_declared_option() -> None:
    """The one document whose keys are data: the option registry itself.

    The rule is the reference's own sentence — "one entry per declared
    option except the bootstrap one, in declaration order" — so the
    sentence is read out of the document and the registry is held
    against it. The bootstrap option is resolved before the merge and
    has no layer to report, which is why it is in no resolution.
    """
    rule = _documents_section(REFERENCE.read_text("utf-8")).split("`Settings.to_dict()`", 1)[1]
    assert "except the\nbootstrap one" in rule
    assert "in declaration order" in rule

    document = SETTINGS.to_dict()
    declared = [option.name for option in api.OPTIONS if not option.bootstrap]
    assert [option.name for option in api.OPTIONS if option.bootstrap] == ["project.dir"]
    assert list(document) == declared, "one entry per declared option, in declaration order"
    for entry in document.values():
        assert sorted(entry) == sorted(DOCUMENTS["Setting"])
    assert json.dumps(document)


def test_a_pending_document_is_owed_rather_than_forgotten() -> None:
    """The documents the reference states and no class answers yet."""
    for name in sorted(set(DOCUMENTS) & set(PENDING)):
        assert not hasattr(api, name), f"{name} exists and is still listed as pending"


def test_an_owed_document_is_still_owed() -> None:
    """:data:`OWED` shrinks by itself, or it stops meaning anything.

    An entry here says "this class is on the surface and answers no
    document yet". The moment it does, the entry is a class that is
    silently not checked — so the arrival of the method fails this test
    instead.
    """
    for name, feature in sorted(OWED.items()):
        owed = getattr(api, name, None)
        assert owed is not None, f"{name} is owed a document and is not exported"
        assert not hasattr(owed, "to_dict"), (
            f"{name} answers a document now — strike it from OWED ({feature}) so it is checked"
        )


# --------------------------------------------------------------------------
# The warning path, end to end
# --------------------------------------------------------------------------


def test_a_warning_is_a_located_document_of_a_published_kind(tmp_path: Path) -> None:
    """What ``on_warning`` hands a client, from a real warning.

    The samples above prove the shape; this proves that the shape is
    what actually leaves the package: a file somebody else can read, the
    warning it draws, and a kind a client can look up.
    """
    secrets = tmp_path / "secrets" / "main.yaml"
    secrets.parent.mkdir(parents=True)
    secrets.write_text("wifi_password: hunter2\n", "utf-8")
    secrets.chmod(0o644)

    found: list[api.Diagnostic] = []
    api.require_secret_file(secrets, key_material=False, on_warning=found.append)

    assert len(found) == 1
    finding = found[0]
    assert finding.kind in api.WARNING_KINDS
    assert finding.severity == "warning"
    assert finding.location.file == secrets, "a warning nobody can place is a line of text again"
    document = finding.to_dict(root=tmp_path)
    assert sorted(document) == sorted(DOCUMENTS["Diagnostic"])
    assert document["file"] == "secrets/main.yaml"
    assert document["hint"]

    # And it is the finding the reference shows, word for word — the
    # example in a document's section is read by whoever writes the
    # client that renders it.
    shown = _example(REFERENCE.read_text("utf-8"), "ValidationResult")["diagnostics"][0]
    written = {
        key: value.replace(str(secrets), "/…/secrets/main.yaml")
        if isinstance(value, str)
        else value
        for key, value in document.items()
    }
    assert written == shown | {"file": "secrets/main.yaml"}
