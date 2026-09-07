# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Resolving a PEP 440 constraint to the one version that satisfies it.

The constraint grammar is PEP 440, amended so a pre-release satisfies a
constraint only when the constraint is itself a pre-release specifier or
pre-releases are explicitly allowed. These tests pin both the happy path
and every refusal the resolver owes a caller.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import VALID_CONFIG, build_package_archive, resolve_file
from mcuhome.model.buildenvironment import DEFAULT_BUILD_TOOLS, DEFAULT_BUILD_WORKSPACE
from mcuhome.model.context import PackagePin
from mcuhome.model.errors import BuildError
from mcuhome.model.sdkindex import DEFAULT_SDK

from mcuhome.workbench.packageregistry import OFFICIAL_BASE_DOMAIN
from mcuhome.workbench.resolve_pins import (
    DEFAULT_SDK_CONSTRAINT,
    SDK_ANY,
    ResolvedPackage,
    concrete_package,
    package_reference,
    resolve_environment,
    resolve_from_index,
    resolve_sdk,
    resolve_sdk_pin,
    resolve_version,
    sdk_constraint,
)

# A stable set with two pre-releases mixed in, so the pre-release rule has
# something to include or drop in every direction.
AVAILABLE = ["2.2.0", "2.3.5", "2.3.6", "2.4.0", "2.4.1", "3.0.0", "2.5.0.dev0", "2.5.0a1"]

# A set whose *highest* version is a pre-release, so admitting or dropping
# pre-releases changes the answer rather than being masked by a later final.
PRE_SET = ["2.4.0", "2.4.1", "2.5.0.dev0"]


def test_resolve_version_picks_the_highest_satisfying() -> None:
    """A constraint resolves to the single highest version in range, not the first."""
    assert resolve_version(">=2.3.6,<3", AVAILABLE) == "2.4.1"


def test_an_exact_pin_resolves_to_that_version() -> None:
    """An exact pin resolves to exactly it, and to nothing else in range."""
    assert resolve_version("==2.3.6", AVAILABLE) == "2.3.6"


def test_the_selected_string_is_returned_verbatim() -> None:
    """The winner comes back as its original spelling, so a caller can key it back."""
    # "2.4" and "2.4.0" are the same PEP 440 version; the string given wins.
    assert resolve_version("==2.4.0", ["2.4"]) == "2.4"


def test_a_prerelease_is_excluded_from_a_stable_constraint() -> None:
    """A stable constraint never resolves to a dev/pre-release.

    ``2.5.0.dev0`` is the highest version in range, but ``>=2.4`` is not a
    pre-release specifier, so the pre-release is not a candidate at all and
    the highest *final* version wins.
    """
    assert resolve_version(">=2.4", PRE_SET) == "2.4.1"


def test_a_prerelease_constraint_admits_the_prerelease() -> None:
    """A pre-release specifier makes pre-releases candidates.

    ``>=2.5.0a1`` is itself a pre-release specifier, so ``2.5.0a1`` is in
    range — the other direction of the rule.
    """
    assert resolve_version(">=2.5.0a1,<3", AVAILABLE) == "2.5.0a1"


def test_explicitly_allowing_prereleases_admits_them() -> None:
    """prereleases=True admits pre-releases a stable constraint would drop."""
    assert resolve_version(">=2.4", PRE_SET, prereleases=True) == "2.5.0.dev0"


def test_an_empty_version_set_is_refused() -> None:
    """Nothing to choose from is a typed refusal, not an empty answer."""
    with pytest.raises(BuildError) as caught:
        resolve_version(">=1.0", [], name="mcuhome-sdk")
    assert "mcuhome-sdk" in caught.value.message
    assert "none" in (caught.value.hint or "")


def test_no_version_in_range_is_refused_and_names_what_was_available() -> None:
    """A constraint nothing satisfies names the constraint and the options."""
    with pytest.raises(BuildError) as caught:
        resolve_version(">=9.0", ["1.0", "2.0"])
    assert ">=9.0" in caught.value.message
    assert "1.0" in (caught.value.hint or "") and "2.0" in (caught.value.hint or "")


def test_a_malformed_constraint_is_refused() -> None:
    """A caret is npm-style, not PEP 440 — refused by grammar, before resolving."""
    with pytest.raises(BuildError) as caught:
        resolve_version("^2.3.6", AVAILABLE)
    assert "PEP 440" in caught.value.message


def test_a_malformed_available_version_is_refused() -> None:
    """A version in the index that is not PEP 440 is a malformed-index refusal."""
    with pytest.raises(BuildError) as caught:
        resolve_version(">=1.0", ["1.0", "not-a-version"])
    assert "PEP 440" in caught.value.message


# --------------------------------------------------------------------------
# Resolving against the static index.json
# --------------------------------------------------------------------------


def _index(**versions: dict[str, object]) -> dict:
    """A package index shaped like scripts/build_sdk_archive.py writes."""
    return {"packages": {"mcuhome-sdk": dict(versions)}}


def test_resolve_from_index_returns_the_pinned_package() -> None:
    """The index resolver picks the version and hands back its entry."""
    index = _index(
        **{
            "0.1.0": {"file": "mcuhome-sdk-0.1.0.tar.zst", "sha256": "a" * 64, "size": 100},
            "0.2.0": {"file": "mcuhome-sdk-0.2.0.tar.zst", "sha256": "b" * 64, "size": 200},
        }
    )
    resolved = resolve_from_index(index, "mcuhome-sdk", ">=0.1")
    assert resolved == ResolvedPackage(
        name="mcuhome-sdk",
        version="0.2.0",
        file="mcuhome-sdk-0.2.0.tar.zst",
        sha256="b" * 64,
        size=200,
    )


def test_an_index_without_the_package_is_refused() -> None:
    """A constraint for a package the index does not carry is a refusal."""
    with pytest.raises(BuildError) as caught:
        resolve_from_index({"packages": {}}, "mcuhome-sdk", ">=0")
    assert "mcuhome-sdk" in caught.value.message


# --------------------------------------------------------------------------
# What a context document records about the pin (E65)
# --------------------------------------------------------------------------
#
# `resolve_sdk` answers more than the three values a pin is, because
# `context.yaml` records two more — the intent and a location hint — and
# both have to be derived from the resolution rather than invented by
# whoever writes the document. Neither may be empty: a context is parsed
# by implementations that are not this one (the build server refuses an
# empty `mcuhome.constraint` or `package.url`), and an empty field cannot
# be told from a dropped one by a reader.


def _sdk_source(directory, *, versions: dict[str, str]) -> None:
    """A source directory holding one index and one file per version."""
    directory.mkdir(parents=True, exist_ok=True)
    entries = {}
    for version, digest in versions.items():
        name = f"mcuhome-sdk-{version}.tar.zst"
        (directory / name).write_bytes(b"not a real archive")
        entries[version] = {"file": name, "sha256": digest, "size": 18}
    (directory / "index.json").write_text(
        json.dumps({"packages": {"mcuhome-sdk": entries}}), encoding="utf-8"
    )


def test_an_unstated_constraint_is_recorded_verbatim_and_empty(tmp_path) -> None:
    """The document records the statement, not a paraphrase.

    The empty specifier is PEP 440's own "any version"; the field is
    informational by contract and the build server accepts it empty.
    Rendering it as ``==<version>`` would erase the difference between
    "any version was fine" and "exactly this one was demanded" — the one
    thing the field preserves.
    """
    _sdk_source(tmp_path / "src", versions={"2.4.0": "a" * 64})
    found = resolve_sdk((tmp_path / "src",))
    assert found.stated == SDK_ANY
    assert found.intent == SDK_ANY
    assert resolve_sdk_pin((tmp_path / "src",))[0] == SDK_ANY


def test_a_stated_constraint_is_recorded_verbatim(tmp_path) -> None:
    """Intent and resolution stay two things wherever there are two (ADR 0018)."""
    _sdk_source(tmp_path / "src", versions={"2.3.6": "a" * 64, "2.4.0": "b" * 64})
    found = resolve_sdk((tmp_path / "src",), constraint="~=2.3")
    assert found.intent == "~=2.3"
    assert found.package.version == "2.4.0"


def test_a_local_source_records_no_url(tmp_path) -> None:
    """No invented location hint — and no local filesystem layout leaked.

    A ``file://`` URI of the source directory would carry the creator's
    home directory and username into a document uploaded to a build
    server. The hint stays empty until a resolution really comes from a
    registry with a public location.
    """
    source = tmp_path / "src"
    _sdk_source(source, versions={"2.4.0": "a" * 64})
    found = resolve_sdk((source,))
    assert found.url == ""


def test_an_unreadable_index_is_an_error_not_a_silent_skip(tmp_path) -> None:
    """A named source with a broken index must not be silently demoted.

    Skipping it would let a lower-precedence source win — a build against
    the wrong SDK instead of an error message. A directory without an
    index stays a legitimate not-here.
    """
    broken = tmp_path / "first"
    broken.mkdir()
    (broken / "index.json").write_text("{not json", encoding="utf-8")
    _sdk_source(tmp_path / "second", versions={"2.4.0": "a" * 64})
    with pytest.raises(BuildError) as caught:
        resolve_sdk((broken, tmp_path / "second"))
    assert "unreadable" in caught.value.message
    assert str(broken) in caught.value.message


# --------------------------------------------------------------------------
# The reference every `sources.*` entry is spelled in
# --------------------------------------------------------------------------


def test_a_reference_keeps_its_base_domain() -> None:
    """One reader for the whole reference, base domain included.

    The constraint used to be read by a parser that split on the last
    slash, which resolved a foreign registry's version correctly and then
    dropped the registry — so the answer was looked up somewhere else.
    Now every part comes out of one call.
    """
    reference = package_reference("packages.example.test/build-tools/mcuhome-build-tools:1.2.0")
    assert reference.base_domain == "packages.example.test"
    assert reference.source == "build-tools"
    assert reference.name == "mcuhome-build-tools"
    assert reference.version == "1.2.0"
    assert reference.sha256 == ""
    assert not reference.pinned


def test_a_reference_that_names_no_registry_means_the_official_one() -> None:
    reference = package_reference("sdk/mcuhome-sdk")
    assert reference.base_domain == OFFICIAL_BASE_DOMAIN
    assert reference.version == ""


def test_a_reference_that_states_a_hash_decides_the_whole_pin() -> None:
    """Version and hash together need no index at all — the offline case."""
    reference = package_reference(
        f"build-workspace/mcuhome-build-workspace:0.1.0@sha256:{'ab' * 32}"
    )
    assert reference.pinned
    assert reference.sha256 == "ab" * 32


@pytest.mark.parametrize(
    "reference",
    ["mcuhome-sdk", "sdk/", "sdk/a/b", ""],
)
def test_a_reference_without_a_source_is_refused(reference: str) -> None:
    """A registry has no default shelf, so a bare package name resolves nowhere."""
    with pytest.raises(BuildError):
        package_reference(reference)


def test_the_sdk_constraint_still_comes_out_of_the_same_reader() -> None:
    """A stated version pins exactly; an unstated one takes the default minor."""
    assert sdk_constraint("sdk/mcuhome-sdk:0.1.9") == ("==0.1.9", None)
    assert sdk_constraint("sdk/mcuhome-sdk") == (DEFAULT_SDK_CONSTRAINT, True)
    assert sdk_constraint("") == (DEFAULT_SDK_CONSTRAINT, True)
    # A foreign registry changes where the answer is looked up, never
    # what the constraint is.
    assert sdk_constraint("packages.example.test/sdk/mcuhome-sdk:0.1.9") == ("==0.1.9", None)


# --------------------------------------------------------------------------
# The environment lock, and the two pins derived from it
# --------------------------------------------------------------------------

WORKSPACE = "mcuhome-build-workspace"
TOOLS = "mcuhome-build-tools"
_LOCK_VERSION = "0.1.10.dev1"


def _sdk_with_lock(directory: Path, *, version: str = "0.1.0", lock: dict | None = None) -> str:
    """A source directory holding an SDK archive whose lock names *lock*.

    Answers the archive's real sha256, which is what a resolution pins
    and what the acquisition checks the bytes against.
    """
    members = {
        "mcuhome-sdk.json": (b'{"sdk": 1}', False),
        "mcuhome/model/__init__.py": (f'__version__ = "{version}"\n'.encode(), False),
    }
    if lock is not None:
        members["build-environment.lock.json"] = (json.dumps(lock).encode(), False)
    directory.mkdir(parents=True, exist_ok=True)
    archive = build_package_archive(members)
    filename = f"mcuhome-sdk-{version}.tar.zst"
    (directory / filename).write_bytes(archive)
    digest = hashlib.sha256(archive).hexdigest()
    index = {
        "packages": {
            "mcuhome-sdk": {version: {"file": filename, "sha256": digest, "size": len(archive)}}
        }
    }
    (directory / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return digest


def _environment_index(directory: Path, *, meta: bool = False, platform: str = "linux-amd64"):
    """Extend a source directory's index with the two environment packages.

    With *meta*, the tools package is published the way it really is: a
    family entry naming one concrete package per architecture, and a hash
    over the members it points at.
    """
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    hashes = {}
    for name in (WORKSPACE, TOOLS):
        payload = f"{name} {_LOCK_VERSION}\n".encode()
        filename = f"{name}-{_LOCK_VERSION}.tar.zst"
        (directory / filename).write_bytes(payload)
        hashes[name] = hashlib.sha256(payload).hexdigest()
        index["packages"][name] = {
            _LOCK_VERSION: {"file": filename, "sha256": hashes[name], "size": len(payload)}
        }
    if meta:
        concrete = f"{TOOLS}_{platform}"
        index["packages"][concrete] = index["packages"].pop(TOOLS)
        entry = {"meta": {"arch": {platform: concrete}}}
        entry["sha256"] = meta_hash(index["packages"], entry["meta"])
        index["packages"][TOOLS] = {_LOCK_VERSION: entry}
        hashes[TOOLS] = entry["sha256"]
        hashes[concrete] = index["packages"][concrete][_LOCK_VERSION]["sha256"]
    (directory / "index.json").write_text(json.dumps(index), encoding="utf-8")
    return hashes


def meta_hash(packages: dict, meta: dict) -> str:
    """The frozen meta-entry hash: the members expanded, canonically encoded.

    Spelled out here rather than imported, because the value under test is
    what a *second* implementation computes — the verifier recomputes it
    the same way, and a test that called the same function would agree
    with it by construction rather than by rule.
    """
    from mcuhome.packagetool.verify import canonical_json

    expanded = {
        dimension: {
            key: {"name": package, "sha256": packages[package][_LOCK_VERSION]["sha256"]}
            for key, package in members.items()
        }
        for dimension, members in meta.items()
    }
    return hashlib.sha256(canonical_json(expanded)).hexdigest()


def test_the_environment_versions_come_out_of_the_sdk_release(tmp_path) -> None:
    """A device that states nothing gets the pair its SDK was tested with.

    This is the whole default: the SDK release carries the versions, the
    index carries the hashes, and nothing about either is written into
    the device.
    """
    source = tmp_path / "src"
    _sdk_with_lock(
        source,
        lock={f"packages.{WORKSPACE}": _LOCK_VERSION, f"packages.{TOOLS}": _LOCK_VERSION},
    )
    hashes = _environment_index(source)
    found = resolve_sdk((source,), constraint="==0.1.0", prereleases=True)
    pin = resolve_environment(
        workspace=DEFAULT_BUILD_WORKSPACE,
        tools=DEFAULT_BUILD_TOOLS,
        sdk_source=DEFAULT_SDK,
        sdk=found,
        sources=(source,),
        work_root=tmp_path / "work",
    )
    assert pin.workspace.name == WORKSPACE
    assert pin.workspace.version == _LOCK_VERSION
    assert pin.workspace.sha256 == hashes[WORKSPACE]
    assert pin.tools.name == TOOLS
    assert pin.tools.sha256 == hashes[TOOLS]


def test_a_family_pin_keeps_the_family_name_and_the_meta_hash(tmp_path) -> None:
    """The normal production shape: the tools entry is the family.

    A meta entry is verified and then **kept**, not followed: the context
    pins the family, and the family's hash covers every platform's
    package — which is what lets one context build the same firmware on
    hosts of two architectures.
    """
    source = tmp_path / "src"
    _sdk_with_lock(
        source,
        lock={f"packages.{WORKSPACE}": _LOCK_VERSION, f"packages.{TOOLS}": _LOCK_VERSION},
    )
    hashes = _environment_index(source, meta=True)
    found = resolve_sdk((source,), constraint="==0.1.0", prereleases=True)
    pin = resolve_environment(
        workspace=DEFAULT_BUILD_WORKSPACE,
        tools=DEFAULT_BUILD_TOOLS,
        sdk_source=DEFAULT_SDK,
        sdk=found,
        sources=(source,),
        work_root=tmp_path / "work",
        platform="linux-amd64",
    )
    assert pin.tools.name == TOOLS
    assert pin.tools.sha256 == hashes[TOOLS]
    # And resolving that pin for this host answers with the platform's
    # own package and its own bytes.
    concrete = concrete_package(
        pin.tools, source="build-tools", sources=(source,), platform="linux-amd64"
    )
    assert concrete.name == f"{TOOLS}_linux-amd64"
    assert concrete.sha256 == hashes[f"{TOOLS}_linux-amd64"]


def test_each_package_may_be_looked_for_in_its_own_directories(tmp_path) -> None:
    """The environment packages are two orders of magnitude larger than the
    SDK, so a machine may well keep them somewhere else.

    The SDK directory here holds only the SDK, and each environment
    package is published in a directory of its own — which resolves only
    if each pin searched the directories it was given rather than the
    SDK's.
    """
    sdk_dir = tmp_path / "sdk"
    _sdk_with_lock(
        sdk_dir,
        lock={f"packages.{WORKSPACE}": _LOCK_VERSION, f"packages.{TOOLS}": _LOCK_VERSION},
    )
    workspace_dir = tmp_path / "workspaces"
    tools_dir = tmp_path / "tools"
    for directory in (workspace_dir, tools_dir):
        directory.mkdir()
        (directory / "index.json").write_text('{"packages": {}}', encoding="utf-8")
    workspace_hashes = _environment_index(workspace_dir)
    tools_hashes = _environment_index(tools_dir)

    found = resolve_sdk((sdk_dir,), constraint="==0.1.0", prereleases=True)
    pin = resolve_environment(
        workspace=DEFAULT_BUILD_WORKSPACE,
        tools=DEFAULT_BUILD_TOOLS,
        sdk_source=DEFAULT_SDK,
        sdk=found,
        sources=(sdk_dir,),
        workspace_sources=(workspace_dir,),
        tools_sources=(tools_dir,),
        work_root=tmp_path / "work",
    )
    assert pin.workspace.sha256 == workspace_hashes[WORKSPACE]
    assert pin.tools.sha256 == tools_hashes[TOOLS]

    # And without the per-package directories the same call cannot pin
    # anything, because the SDK's directory publishes neither.
    with pytest.raises(BuildError):
        resolve_environment(
            workspace=DEFAULT_BUILD_WORKSPACE,
            tools=DEFAULT_BUILD_TOOLS,
            sdk_source=DEFAULT_SDK,
            sdk=found,
            sources=(sdk_dir,),
            work_root=tmp_path / "work",
        )


def test_a_device_override_replaces_one_derivation_only(tmp_path) -> None:
    """Each `sources.*` entry overrides its own package and nothing else."""
    source = tmp_path / "src"
    _sdk_with_lock(
        source,
        lock={f"packages.{WORKSPACE}": _LOCK_VERSION, f"packages.{TOOLS}": _LOCK_VERSION},
    )
    hashes = _environment_index(source)
    found = resolve_sdk((source,), constraint="==0.1.0", prereleases=True)
    pin = resolve_environment(
        workspace=DEFAULT_BUILD_WORKSPACE,
        tools=f"build-tools/{TOOLS}:{_LOCK_VERSION}@sha256:{'cd' * 32}",
        sdk_source=DEFAULT_SDK,
        sdk=found,
        sources=(source,),
        work_root=tmp_path / "work",
    )
    # The stated half is the device's word, hash and all; the other half
    # still comes from the release.
    assert pin.tools.sha256 == "cd" * 32
    assert pin.workspace.sha256 == hashes[WORKSPACE]


def _source_with_environment(tmp_path: Path):
    """A source directory holding the SDK release and the two packages."""
    source = tmp_path / "src"
    _sdk_with_lock(
        source,
        lock={f"packages.{WORKSPACE}": _LOCK_VERSION, f"packages.{TOOLS}": _LOCK_VERSION},
    )
    return source, _environment_index(source)


def _pins_of(model, source: Path, work_root: Path):
    """The two pins a device model resolves to, through the production path.

    The same three model fields ``create_build_context`` hands over, in
    the same order, so what this asserts is what a build would get.
    """
    constraint, prereleases = sdk_constraint(model.sources.sdk)
    found = resolve_sdk((source,), constraint=constraint, prereleases=prereleases)
    return (constraint, prereleases), resolve_environment(
        workspace=model.sources.build_workspace,
        tools=model.sources.build_tools,
        sdk_source=model.sources.sdk,
        sdk=found,
        sources=(source,),
        work_root=work_root,
    )


def test_a_device_without_sources_states_no_pin_and_gets_the_defaults(
    tmp_path, write_config
) -> None:
    """The ordinary device: nothing written down, everything resolved.

    A device file that names no ``sources:`` carries the three default
    references — a package and no version — so the SDK resolves against
    the minor this workbench was released alongside and the environment
    against what that SDK release states. Nothing is written back into the
    device, which is what keeps it from being frozen onto whatever was
    current on the day it was created.
    """
    source, hashes = _source_with_environment(tmp_path)
    model = resolve_file(write_config(VALID_CONFIG))

    assert (model.sources.sdk, model.sources.build_workspace, model.sources.build_tools) == (
        DEFAULT_SDK,
        DEFAULT_BUILD_WORKSPACE,
        DEFAULT_BUILD_TOOLS,
    )
    assert sdk_constraint(model.sources.sdk) == (DEFAULT_SDK_CONSTRAINT, True)
    _, pin = _pins_of(model, source, tmp_path / "work")
    assert pin.workspace.sha256 == hashes[WORKSPACE]
    assert pin.tools.sha256 == hashes[TOOLS]


def test_a_device_file_can_pin_the_sdk(tmp_path, write_config) -> None:
    """``sources.sdk`` in a device file decides the SDK constraint.

    The device names a version, so the default minor does not apply and
    the pre-release rule goes back to the ordinary one — which is
    :func:`sdk_constraint`'s answer, not this test's own arithmetic.
    """
    source, hashes = _source_with_environment(tmp_path)
    model = resolve_file(write_config(VALID_CONFIG + f"\nsources:\n  sdk: {DEFAULT_SDK}:0.1.0\n"))

    assert model.sources.sdk == f"{DEFAULT_SDK}:0.1.0"
    stated, pin = _pins_of(model, source, tmp_path / "work")
    assert stated == ("==0.1.0", None)
    # The other two entries were not stated and still come from the release.
    assert pin.workspace.sha256 == hashes[WORKSPACE]
    assert pin.tools.sha256 == hashes[TOOLS]


@pytest.mark.parametrize(
    ("key", "package"),
    [("build_workspace", WORKSPACE), ("build_tools", TOOLS)],
)
def test_a_device_file_can_pin_one_environment_package(
    tmp_path, write_config, key: str, package: str
) -> None:
    """Each environment override reaches the resolution, and alone.

    The stated package is the device's word — version and hash, so no
    index is consulted for it at all — while the other one still comes
    out of the SDK release's lock. That the two are independent is the
    property: a device that pins one package must not silently pin the
    other to today's version with it.
    """
    source, hashes = _source_with_environment(tmp_path)
    kind = "build-workspace" if key == "build_workspace" else "build-tools"
    reference = f"{kind}/{package}:{_LOCK_VERSION}@sha256:{'cd' * 32}"
    model = resolve_file(write_config(VALID_CONFIG + f"\nsources:\n  {key}: {reference}\n"))

    assert getattr(model.sources, key) == reference
    _, pin = _pins_of(model, source, tmp_path / "work")
    stated, derived = (
        (pin.workspace, pin.tools) if key == "build_workspace" else (pin.tools, pin.workspace)
    )
    other = TOOLS if key == "build_workspace" else WORKSPACE
    assert stated.sha256 == "cd" * 32
    assert derived.sha256 == hashes[other]


def test_a_release_without_a_lock_is_refused_legibly(tmp_path) -> None:
    """An SDK that does not say which environment it wants cannot be guessed at."""
    source = tmp_path / "src"
    _sdk_with_lock(source, lock=None)
    found = resolve_sdk((source,), constraint="==0.1.0", prereleases=True)
    with pytest.raises(BuildError) as caught:
        resolve_environment(
            workspace=DEFAULT_BUILD_WORKSPACE,
            tools=DEFAULT_BUILD_TOOLS,
            sdk_source=DEFAULT_SDK,
            sdk=found,
            sources=(source,),
            work_root=tmp_path / "work",
        )
    assert "build-environment.lock.json" in caught.value.message
    assert "sources.build_workspace" in caught.value.hint


def test_a_lock_that_names_no_such_package_is_refused(tmp_path) -> None:
    """The refusal names the package and lists what the release does state."""
    source = tmp_path / "src"
    _sdk_with_lock(source, lock={f"packages.{WORKSPACE}": _LOCK_VERSION})
    _environment_index(source)
    found = resolve_sdk((source,), constraint="==0.1.0", prereleases=True)
    with pytest.raises(BuildError) as caught:
        resolve_environment(
            workspace=DEFAULT_BUILD_WORKSPACE,
            tools=DEFAULT_BUILD_TOOLS,
            sdk_source=DEFAULT_SDK,
            sdk=found,
            sources=(source,),
            work_root=tmp_path / "work",
        )
    assert TOOLS in caught.value.message
    assert WORKSPACE in caught.value.hint


def test_a_pin_the_index_disagrees_with_is_refused(tmp_path) -> None:
    """Same version, other bytes, is a refusal and never a substitution.

    The guard against a mirror that publishes different bytes under the
    version a context was created against.
    """
    source = tmp_path / "src"
    _sdk_with_lock(
        source,
        lock={f"packages.{WORKSPACE}": _LOCK_VERSION, f"packages.{TOOLS}": _LOCK_VERSION},
    )
    _environment_index(source)
    with pytest.raises(BuildError) as caught:
        concrete_package(
            PackagePin(name=WORKSPACE, version=_LOCK_VERSION, sha256="ff" * 32),
            source="build-workspace",
            sources=(source,),
        )
    assert "pinned to" in caught.value.message


def test_a_fully_pinned_reference_needs_no_index_at_all(tmp_path) -> None:
    """The offline escape hatch: a device that states version and hash.

    Nothing is looked up — not the lock, not an index — so an operator
    who has the bytes can build with no package source configured for
    them at all.
    """
    source = tmp_path / "src"
    _sdk_with_lock(source, lock={})
    found = resolve_sdk((source,), constraint="==0.1.0", prereleases=True)
    pin = resolve_environment(
        workspace=f"build-workspace/{WORKSPACE}:1.0.0@sha256:{'11' * 32}",
        tools=f"build-tools/{TOOLS}:1.0.0@sha256:{'22' * 32}",
        sdk_source=DEFAULT_SDK,
        sdk=found,
        sources=(source,),
        work_root=tmp_path / "work",
    )
    assert (pin.workspace.version, pin.workspace.sha256) == ("1.0.0", "11" * 32)
    assert (pin.tools.version, pin.tools.sha256) == ("1.0.0", "22" * 32)


def test_a_second_registry_for_the_environment_packages_is_refused(tmp_path) -> None:
    """A build reads one package host, and the refusal says which two it was given.

    A trust anchor is per base domain and the resolution is handed one
    client — the SDK's. A device that pointed its environment packages at
    another domain would have them looked up on the SDK's host, which is a
    pin resolved against a registry nobody chose. Refused, not
    substituted.
    """
    source = tmp_path / "src"
    _sdk_with_lock(source, lock={f"packages.{WORKSPACE}": _LOCK_VERSION})
    _environment_index(source)
    found = resolve_sdk((source,), constraint="==0.1.0", prereleases=True)
    with pytest.raises(BuildError) as caught:
        resolve_environment(
            workspace=f"packages.example.test/build-workspace/{WORKSPACE}",
            tools=DEFAULT_BUILD_TOOLS,
            sdk_source=DEFAULT_SDK,
            sdk=found,
            sources=(source,),
            work_root=tmp_path / "work",
        )
    assert "packages.example.test" in caught.value.message
    assert OFFICIAL_BASE_DOMAIN in caught.value.message


def test_a_fully_pinned_reference_may_name_any_registry(tmp_path) -> None:
    """It needs no host at all, so there is nothing for a second one to break."""
    source = tmp_path / "src"
    _sdk_with_lock(source, lock={})
    found = resolve_sdk((source,), constraint="==0.1.0", prereleases=True)
    pin = resolve_environment(
        workspace=f"packages.example.test/build-workspace/{WORKSPACE}:1.0.0@sha256:{'11' * 32}",
        tools=f"other.example.test/build-tools/{TOOLS}:1.0.0@sha256:{'22' * 32}",
        sdk_source=DEFAULT_SDK,
        sdk=found,
        sources=(source,),
        work_root=tmp_path / "work",
    )
    assert pin.workspace.sha256 == "11" * 32


def test_a_source_that_holds_the_package_but_not_the_version_is_not_the_end(tmp_path) -> None:
    """A stale operator directory must not stop a build the next source answers.

    The SDK's own resolution has always fallen through such a source; the
    environment packages do the same, or one forgotten mirror directory
    turns every build into a refusal.
    """
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "index.json").write_text(
        json.dumps(
            {
                "packages": {
                    WORKSPACE: {"0.0.9": {"file": "old.tar.zst", "sha256": "9" * 64, "size": 1}}
                }
            }
        ),
        encoding="utf-8",
    )
    source = tmp_path / "src"
    _sdk_with_lock(
        source,
        lock={f"packages.{WORKSPACE}": _LOCK_VERSION, f"packages.{TOOLS}": _LOCK_VERSION},
    )
    hashes = _environment_index(source)
    found = resolve_sdk((source,), constraint="==0.1.0", prereleases=True)
    pin = resolve_environment(
        workspace=DEFAULT_BUILD_WORKSPACE,
        tools=DEFAULT_BUILD_TOOLS,
        sdk_source=DEFAULT_SDK,
        sdk=found,
        sources=(stale, source),
        work_root=tmp_path / "work",
    )
    assert pin.workspace.sha256 == hashes[WORKSPACE]


def test_a_source_that_publishes_other_bytes_under_the_pinned_version_is_refused(
    tmp_path,
) -> None:
    """The one thing that is never shopped around for: same version, other bytes."""
    source = tmp_path / "src"
    _sdk_with_lock(source, lock={})
    hashes = _environment_index(source)
    del hashes
    with pytest.raises(BuildError) as caught:
        concrete_package(
            PackagePin(name=WORKSPACE, version=_LOCK_VERSION, sha256="ff" * 32),
            source="build-workspace",
            sources=(source, source),
        )
    assert "pinned to" in caught.value.message
