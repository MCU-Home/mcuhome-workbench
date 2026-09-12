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
from types import SimpleNamespace

import pytest
from conftest import VALID_CONFIG, build_package_archive, package_meta, resolve_file
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
# What a context document records about the pin
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
    """Intent and resolution stay two things wherever there are two."""
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
# The chain: the SDK requires a workspace, the workspace requires tools
# --------------------------------------------------------------------------

SDK = "mcuhome-sdk"
WORKSPACE = "mcuhome-build-workspace"
TOOLS = "mcuhome-build-tools"
PLATFORM = "linux-amd64"
CONCRETE_TOOLS = f"{TOOLS}_{PLATFORM}"


class Source:
    """A package source directory a test publishes into.

    The production shape and nothing simulated: every archive gets its
    ``<archive>.meta.json`` beside it and the index records it under
    ``meta_file``, because that is what a chain resolution reads. What a
    test varies is what the documents *say* — which constraint, which
    versions, and whether a version states anything at all.
    """

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory)
        self.path.mkdir(parents=True, exist_ok=True)
        self.packages: dict[str, dict[str, dict]] = {}
        self.hashes: dict[str, str] = {}
        self._write()

    def sdk(
        self, version: str = "0.1.0", *, requires: dict | None = None, meta: bool = True
    ) -> str:
        """One SDK release, whose archive carries the first link of the chain."""
        members = {
            "mcuhome-sdk.json": (b'{"sdk": 1}', False),
            "mcuhome/model/__init__.py": (f'__version__ = "{version}"\n'.encode(), False),
        }
        if meta:
            members["meta.json"] = (package_meta("mcuhome-sdk", version, requires=requires), False)
        archive = build_package_archive(members)
        filename = f"mcuhome-sdk-{version}.tar.zst"
        (self.path / filename).write_bytes(archive)
        digest = hashlib.sha256(archive).hexdigest()
        self.packages.setdefault("mcuhome-sdk", {})[version] = {
            "file": filename,
            "sha256": digest,
            "size": len(archive),
        }
        self.hashes[f"mcuhome-sdk {version}"] = digest
        self._write()
        return digest

    def publish(
        self,
        name: str,
        version: str,
        *,
        requires: dict | None = None,
        meta: bool = True,
        architecture: str | None = None,
        broken: str = "",
    ) -> str:
        """One package archive, its sidecar, and the index entry for both.

        *meta* ``False`` publishes a version that says nothing about what
        it requires — the shape of everything published before meta files
        existed. *broken* replaces what the index records the sidecar's
        hash as, which is how a test arranges bytes that do not verify.
        """
        payload = f"{name} {version}\n".encode()
        filename = f"{name}-{version}.tar.zst"
        (self.path / filename).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        entry: dict = {"file": filename, "sha256": digest, "size": len(payload)}
        if meta:
            document = package_meta(
                name.split("_", 1)[0], version, requires=requires, architecture=architecture
            )
            (self.path / f"{filename}.meta.json").write_bytes(document)
            entry["meta_file"] = {
                "file": f"{filename}.meta.json",
                "sha256": broken or hashlib.sha256(document).hexdigest(),
                "size": len(document),
            }
        self.packages.setdefault(name, {})[version] = entry
        self.hashes[f"{name} {version}"] = digest
        self._write()
        return digest

    def family(self, family: str, version: str, *, platform: str = PLATFORM) -> str:
        """The meta entry that makes a family name resolve per platform."""
        members = {platform: f"{family}_{platform}"}
        entry = {"meta": {"arch": members}}
        entry["sha256"] = meta_hash(self.packages, entry["meta"], version)
        self.packages.setdefault(family, {})[version] = entry
        self.hashes[f"{family} {version}"] = entry["sha256"]
        self._write()
        return entry["sha256"]

    def hash_of(self, name: str, version: str) -> str:
        return self.hashes[f"{name} {version}"]

    def _write(self) -> None:
        (self.path / "index.json").write_text(
            json.dumps({"packages": self.packages}), encoding="utf-8"
        )


def meta_hash(packages: dict, meta: dict, version: str) -> str:
    """The frozen meta-entry hash: the members expanded, canonically encoded.

    Spelled out here rather than imported, because the value under test is
    what a *second* implementation computes — the verifier recomputes it
    the same way, and a test that called the same function would agree
    with it by construction rather than by rule.
    """
    from mcuhome.packagetool.verify import canonical_json

    expanded = {
        dimension: {
            key: {"name": package, "sha256": packages[package][version]["sha256"]}
            for key, package in members.items()
        }
        for dimension, members in meta.items()
    }
    return hashlib.sha256(canonical_json(expanded)).hexdigest()


def chained(tmp_path: Path, *, sdk_requires: str = "~=0.1.0", tools_requires: str = "~=0.1.0"):
    """The ordinary source: one SDK, one workspace, one tools package."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: sdk_requires})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: tools_requires})
    source.publish(TOOLS, "0.1.0")
    return source


def resolved(source: Source, tmp_path: Path, **kwargs):
    """What a device with no ``sources:`` at all resolves to, through the chain.

    *source* is a single directory that, unless a test says otherwise,
    publishes all three package kinds — so it is the default for
    ``workspace_sources`` and ``tools_sources`` too, exactly as it is for
    ``sources``. Each key is still its own key: a test that keeps its
    packages apart passes its own directories and this default never
    applies to them.
    """
    found = resolve_sdk((source.path,), constraint="==0.1.0", prereleases=True)
    return resolve_environment(
        workspace=kwargs.pop("workspace", DEFAULT_BUILD_WORKSPACE),
        tools=kwargs.pop("tools", DEFAULT_BUILD_TOOLS),
        sdk_source=kwargs.pop("sdk_source", DEFAULT_SDK),
        sdk=found,
        sources=kwargs.pop("sources", (source.path,)),
        workspace_sources=kwargs.pop("workspace_sources", (source.path,)),
        tools_sources=kwargs.pop("tools_sources", (source.path,)),
        work_root=tmp_path / "work",
        **kwargs,
    )


def test_the_chain_resolves_two_hops_from_the_index(tmp_path) -> None:
    """The whole default: SDK to workspace to tools, one constraint per link.

    The SDK's own meta file states which *range* of build workspaces it
    was built with; the newest published workspace in that range wins,
    and that package's own meta file states the range of build tools.
    Nothing in the middle is a version somebody wrote down twice.
    """
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.4", requires={TOOLS: "~=0.2.0"})
    source.publish(WORKSPACE, "0.2.0", requires={TOOLS: "~=0.3.0"})
    source.publish(TOOLS, "0.1.9")
    source.publish(TOOLS, "0.2.0")
    source.publish(TOOLS, "0.2.3")
    source.publish(TOOLS, "0.3.0")

    pin = resolved(source, tmp_path)
    # 0.2.0 is outside the SDK's ~=0.1.0, so the newest inside it wins —
    # and the tools follow *that* workspace's own constraint, not the
    # newest tools package there is.
    assert (pin.workspace.name, pin.workspace.version) == (WORKSPACE, "0.1.4")
    assert pin.workspace.sha256 == source.hash_of(WORKSPACE, "0.1.4")
    assert (pin.tools.name, pin.tools.version) == (TOOLS, "0.2.3")
    assert pin.tools.sha256 == source.hash_of(TOOLS, "0.2.3")


def test_a_range_constraint_resolves_the_same_way(tmp_path) -> None:
    """``>=,<`` is PEP 440 as much as ``~=`` is, in a meta file too."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: ">=0.1.4,<0.2"})
    for version in ("0.1.0", "0.1.4", "0.1.9", "0.2.0"):
        source.publish(WORKSPACE, version, requires={TOOLS: ">=0.1,<0.3"})
    source.publish(TOOLS, "0.2.7")
    source.publish(TOOLS, "0.3.0")

    pin = resolved(source, tmp_path)
    assert pin.workspace.version == "0.1.9"
    assert pin.tools.version == "0.2.7"


def test_a_version_without_a_meta_file_is_no_candidate(tmp_path) -> None:
    """A version that does not say what it requires cannot be resolved through.

    It would leave the next stage with nothing to go on — so the newest
    version *that states something* wins, even where a newer one exists.
    """
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.7", meta=False)
    source.publish(TOOLS, "0.1.0")

    pin = resolved(source, tmp_path)
    assert pin.workspace.version == "0.1.0"


def test_a_stage_whose_versions_all_state_nothing_is_refused_legibly(tmp_path) -> None:
    """Everything published before meta files existed, in one sentence."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", meta=False)
    source.publish(WORKSPACE, "0.1.7", meta=False)

    with pytest.raises(BuildError) as caught:
        resolved(source, tmp_path)
    assert "says what it requires" in caught.value.message
    assert WORKSPACE in caught.value.message
    assert "meta.json" in caught.value.hint
    assert "0.1.7" in caught.value.hint
    assert "sources.build_workspace" in caught.value.hint


def test_nothing_satisfying_the_constraint_is_refused_with_what_there_is(tmp_path) -> None:
    """The other refusal: versions with meta files, none of them in range."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.9.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})

    with pytest.raises(BuildError) as caught:
        resolved(source, tmp_path)
    assert "~=0.9.0" in caught.value.message
    assert "0.1.0" in caught.value.hint


def test_a_release_that_states_no_requirement_is_refused(tmp_path) -> None:
    """A package that ends the chain early cannot have a constraint guessed for it."""
    source = Source(tmp_path / "src")
    source.sdk(requires=None)
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})

    with pytest.raises(BuildError) as caught:
        resolved(source, tmp_path)
    assert WORKSPACE in caught.value.message
    assert "sources" in caught.value.hint


def test_a_release_without_a_meta_file_is_refused_legibly(tmp_path) -> None:
    """An SDK that does not say which environment it wants cannot be guessed at."""
    source = Source(tmp_path / "src")
    source.sdk(meta=False)
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})

    with pytest.raises(BuildError) as caught:
        resolved(source, tmp_path)
    assert "meta.json" in caught.value.message
    assert "sources.build_workspace" in caught.value.hint


def test_a_meta_file_whose_bytes_do_not_verify_is_refused(tmp_path) -> None:
    """The hash the index records is what decides which bytes are the right ones."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"}, broken="ff" * 32)

    with pytest.raises(BuildError) as caught:
        resolved(source, tmp_path)
    assert "hashes to" in caught.value.message


def test_a_source_that_lists_a_meta_file_and_does_not_carry_it_is_refused(tmp_path) -> None:
    """An incomplete copy is said out loud rather than resolved around.

    The index is the record of what a directory holds; a sidecar it names
    and does not have is a synchronisation that stopped half way, and
    quietly falling through to the next source would hide it for as long
    as another source answers.
    """
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    (source.path / f"{WORKSPACE}-0.1.0.tar.zst.meta.json").unlink()

    with pytest.raises(BuildError) as caught:
        resolved(source, tmp_path)
    assert "does not carry it" in caught.value.message
    assert "Synchronise" in caught.value.hint


def test_a_meta_file_of_another_schema_is_refused(tmp_path) -> None:
    """A reader that guessed at a shape it does not know would resolve from a
    document it misunderstood."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    sidecar = source.path / f"{WORKSPACE}-0.1.0.tar.zst.meta.json"
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    document["schema"] = 2
    payload = json.dumps(document).encode()
    sidecar.write_bytes(payload)
    source.packages[WORKSPACE]["0.1.0"]["meta_file"] = {
        "file": sidecar.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }
    source._write()

    with pytest.raises(BuildError) as caught:
        resolved(source, tmp_path)
    assert "schema" in caught.value.message


def test_a_meta_file_that_describes_another_package_is_refused(tmp_path) -> None:
    """A sidecar is held against the archive it sits beside."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    sidecar = source.path / f"{WORKSPACE}-0.1.0.tar.zst.meta.json"
    payload = package_meta("mcuhome-something-else", "0.1.0", requires={TOOLS: "~=0.1.0"})
    sidecar.write_bytes(payload)
    source.packages[WORKSPACE]["0.1.0"]["meta_file"] = {
        "file": sidecar.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }
    source._write()

    with pytest.raises(BuildError) as caught:
        resolved(source, tmp_path)
    assert "mcuhome-something-else" in caught.value.message


def test_a_host_prefixed_requirement_names_the_package_it_is_about(tmp_path) -> None:
    """``requires`` keys may carry the host the package comes from.

    The constraint is the same constraint; the prefix says where, and a
    build that reads one host resolves it there.
    """
    source = Source(tmp_path / "src")
    source.sdk(requires={f"{OFFICIAL_BASE_DOMAIN}/{WORKSPACE}": "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={f"{OFFICIAL_BASE_DOMAIN}/{TOOLS}": "~=0.1.0"})
    source.publish(TOOLS, "0.1.0")

    pin = resolved(source, tmp_path)
    assert (pin.workspace.version, pin.tools.version) == ("0.1.0", "0.1.0")


# --------------------------------------------------------------------------
# What a device may override, and what it is told about it
# --------------------------------------------------------------------------


def test_a_device_may_narrow_the_range_with_a_constraint(tmp_path) -> None:
    """``:~=0.1.4`` is a device deciding for itself inside what the chain allows."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    for version in ("0.1.0", "0.1.4", "0.1.9"):
        source.publish(WORKSPACE, version, requires={TOOLS: "~=0.1.0"})
    source.publish(TOOLS, "0.1.0")

    lines: list[str] = []
    pin = resolved(source, tmp_path, workspace=f"{WORKSPACE}:>=0.1.4,<0.1.9", on_line=lines.append)
    assert pin.workspace.version == "0.1.4"
    # Inside the declared range, so nothing to say about it.
    assert lines == []


def test_an_override_outside_the_declared_range_builds_and_says_so(tmp_path) -> None:
    """Never a refusal: the stage above knows what it was tested with, not
    what is allowed."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    source.publish(WORKSPACE, "0.9.0", requires={TOOLS: "~=0.1.0"})
    source.publish(TOOLS, "0.1.0")

    lines: list[str] = []
    pin = resolved(source, tmp_path, workspace=f"{WORKSPACE}:0.9.0", on_line=lines.append)
    assert pin.workspace.version == "0.9.0"
    assert len(lines) == 1
    assert lines[0].startswith("Note: ")
    assert "sources.build_workspace" in lines[0]
    assert "~=0.1.0" in lines[0]
    assert "0.9.0" in lines[0]


def test_an_override_naming_another_package_builds_and_says_so(tmp_path) -> None:
    """The chain cannot speak about a package nobody required."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish("acme-workspace", "2.0.0", requires={TOOLS: "~=0.1.0"})
    source.publish(TOOLS, "0.1.0")

    lines: list[str] = []
    pin = resolved(source, tmp_path, workspace="acme-workspace", on_line=lines.append)
    assert (pin.workspace.name, pin.workspace.version) == ("acme-workspace", "2.0.0")
    assert len(lines) == 1
    assert f"{SDK} 0.1.0 requires {WORKSPACE} ~=0.1.0" in lines[0]
    assert "acme-workspace 2.0.0" in lines[0]


def test_naming_this_platform_s_package_keeps_the_family_s_constraint(tmp_path) -> None:
    """``mcuhome-build-tools_linux-amd64`` is the required family, spelled out.

    A requirement is stated about the family, and a device that names one
    platform's package of it is asking for the same thing with the
    coordinate written down. Reading the two as different packages would
    silently drop the declared range — and resolve to whatever the newest
    published version happens to be, which is the opposite of what
    naming a package more precisely means.
    """
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    source.publish(CONCRETE_TOOLS, "0.1.0", architecture=PLATFORM)
    source.publish(CONCRETE_TOOLS, "0.9.9", architecture=PLATFORM)

    lines: list[str] = []
    pin = resolved(
        source,
        tmp_path,
        tools=f"build-tools/{CONCRETE_TOOLS}",
        platform=PLATFORM,
        on_line=lines.append,
    )
    assert (pin.tools.name, pin.tools.version) == (CONCRETE_TOOLS, "0.1.0")
    # Inside what the workspace declared, so there is nothing to say.
    assert lines == []


def test_a_device_that_names_one_host_is_not_told_it_named_two(tmp_path) -> None:
    """A reference that says nothing takes the host of whoever required it.

    A device pointing its *SDK* at another registry and leaving the
    environment packages alone has named one host, not two — and the
    chain says where those packages come from: the SDK's own. Refusing it
    over a second host would refuse a device for something nobody wrote.
    """
    source = chained(tmp_path)
    pin = resolved(
        source,
        tmp_path,
        sdk_source=f"packages.example.test/sdk/{SDK}",
    )
    assert pin.workspace.version == "0.1.0"
    assert pin.tools.version == "0.1.0"


def test_an_override_pinning_a_hash_selects_that_archive(tmp_path) -> None:
    """``@sha256:`` with no version: the bytes decide, the index says which
    release they are — and the chain goes on from that version's meta file."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    wanted = source.publish(WORKSPACE, "0.1.4", requires={TOOLS: "~=0.2.0"})
    source.publish(TOOLS, "0.1.0")
    source.publish(TOOLS, "0.2.0")

    pin = resolved(source, tmp_path, workspace=f"@sha256:{wanted}")
    assert (pin.workspace.version, pin.workspace.sha256) == ("0.1.4", wanted)
    assert pin.tools.version == "0.2.0"


def test_a_fully_pinned_reference_needs_no_index_at_all(tmp_path) -> None:
    """The offline escape hatch: a device that states version and hash.

    Nothing is looked up — not the SDK's meta file, not an index — so an
    operator who has the bytes can build with no package source
    configured for them at all. The chain ends there, so the next stage
    has to be stated too.
    """
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})

    pin = resolved(
        source,
        tmp_path,
        workspace=f"build-workspace/{WORKSPACE}:1.0.0@sha256:{'11' * 32}",
        tools=f"build-tools/{TOOLS}:1.0.0@sha256:{'22' * 32}",
    )
    assert (pin.workspace.version, pin.workspace.sha256) == ("1.0.0", "11" * 32)
    assert (pin.tools.version, pin.tools.sha256) == ("1.0.0", "22" * 32)


def test_a_pinned_workspace_leaves_the_tools_unstated_and_says_so(tmp_path) -> None:
    """A package that was never looked up cannot say what it needs."""
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(TOOLS, "0.1.0")

    with pytest.raises(BuildError) as caught:
        resolved(
            source,
            tmp_path,
            workspace=f"build-workspace/{WORKSPACE}:1.0.0@sha256:{'11' * 32}",
        )
    assert TOOLS in caught.value.message
    assert "sources.build_tools" in caught.value.hint


def test_a_device_override_replaces_one_derivation_only(tmp_path) -> None:
    """Each ``sources.*`` entry overrides its own package and nothing else."""
    source = chained(tmp_path)
    pin = resolved(source, tmp_path, tools=f"build-tools/{TOOLS}:0.1.0@sha256:{'cd' * 32}")
    assert pin.tools.sha256 == "cd" * 32
    assert pin.workspace.sha256 == source.hash_of(WORKSPACE, "0.1.0")


# --------------------------------------------------------------------------
# Where the packages are looked for
# --------------------------------------------------------------------------


def test_a_family_pin_keeps_the_family_name_and_the_meta_hash(tmp_path) -> None:
    """The normal production shape: the tools entry is the family.

    A meta entry is verified and then **kept**, not followed: the context
    pins the family, and the family's hash covers every platform's
    package — which is what lets one context build the same firmware on
    hosts of two architectures.
    """
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    source.publish(CONCRETE_TOOLS, "0.1.0", architecture=PLATFORM)
    family = source.family(TOOLS, "0.1.0")

    pin = resolved(source, tmp_path, platform=PLATFORM)
    assert pin.tools.name == TOOLS
    assert pin.tools.sha256 == family
    # And resolving that pin for this host answers with the platform's
    # own package and its own bytes.
    concrete = concrete_package(
        pin.tools, source="build-tools", sources=(source.path,), platform=PLATFORM
    )
    assert concrete.name == CONCRETE_TOOLS
    assert concrete.sha256 == source.hash_of(CONCRETE_TOOLS, "0.1.0")


def test_a_local_directory_without_a_family_entry_still_answers(tmp_path) -> None:
    """An operator's directory holds what one machine needs, and that is enough.

    A directory that carries this host's tools package and no family
    entry used to be skipped silently, and the pin resolved through the
    registry instead — which is the opposite of what pointing at a
    directory means. It is pinned by the concrete name, because that is
    the name those bytes are published under there.
    """
    source = Source(tmp_path / "src")
    source.sdk(requires={WORKSPACE: "~=0.1.0"})
    source.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    tools = Source(tmp_path / "tools")
    tools.publish(CONCRETE_TOOLS, "0.1.0", architecture=PLATFORM)

    pin = resolved(source, tmp_path, tools_sources=(tools.path,), platform=PLATFORM)
    assert pin.tools.name == CONCRETE_TOOLS
    assert pin.tools.version == "0.1.0"
    assert pin.tools.sha256 == tools.hash_of(CONCRETE_TOOLS, "0.1.0")


def test_each_package_may_be_looked_for_in_its_own_directories(tmp_path) -> None:
    """The environment packages are two orders of magnitude larger than the
    SDK, so a machine may well keep them somewhere else.

    The SDK directory here holds only the SDK, and each environment
    package is published in a directory of its own — which resolves only
    if each stage searched the directories it was given rather than the
    SDK's.
    """
    sdk_dir = Source(tmp_path / "sdk")
    sdk_dir.sdk(requires={WORKSPACE: "~=0.1.0"})
    workspaces = Source(tmp_path / "workspaces")
    workspaces.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    tools = Source(tmp_path / "tools")
    tools.publish(TOOLS, "0.1.0")

    pin = resolved(
        sdk_dir,
        tmp_path,
        workspace_sources=(workspaces.path,),
        tools_sources=(tools.path,),
    )
    assert pin.workspace.sha256 == workspaces.hash_of(WORKSPACE, "0.1.0")
    assert pin.tools.sha256 == tools.hash_of(TOOLS, "0.1.0")

    # And without the per-package directories the same call cannot pin
    # anything, because the SDK's directory publishes neither.
    with pytest.raises(BuildError):
        resolved(sdk_dir, tmp_path)


def test_a_source_that_holds_the_package_but_not_the_version_is_not_the_end(tmp_path) -> None:
    """A stale operator directory must not stop a build the next source answers.

    The SDK's own resolution has always fallen through such a source; the
    environment packages do the same, or one forgotten mirror directory
    turns every build into a refusal.
    """
    stale = Source(tmp_path / "stale")
    stale.publish(WORKSPACE, "0.0.9", requires={TOOLS: "~=0.1.0"})
    source = chained(tmp_path)

    pin = resolved(source, tmp_path, sources=(stale.path, source.path))
    assert pin.workspace.sha256 == source.hash_of(WORKSPACE, "0.1.0")


def test_a_second_registry_is_refused_where_no_host_can_be_opened(tmp_path) -> None:
    """A build server resolves for one host by decision, and says which two it got.

    A trust anchor is per base domain. Without a way to open the second
    domain's registry, a reference pointing there would be looked up on
    the first — which is a pin resolved against a registry nobody chose.
    """
    source = chained(tmp_path)
    with pytest.raises(BuildError) as caught:
        resolved(
            source,
            tmp_path,
            workspace=f"packages.example.test/build-workspace/{WORKSPACE}",
        )
    assert "packages.example.test" in caught.value.message
    assert OFFICIAL_BASE_DOMAIN in caught.value.message


def test_a_second_registry_resolves_through_its_own_host(tmp_path) -> None:
    """A device that points one package at another registry is honoured there.

    The host decides which trust anchor and which mirrors apply, so the
    reference is resolved by a client built for *that* domain — and the
    build says once that the device went outside what the SDK declared.
    """
    source = chained(tmp_path)
    foreign = Source(tmp_path / "foreign")
    foreign.publish(WORKSPACE, "0.1.0", requires={TOOLS: "~=0.1.0"})
    # The workspace is looked for where the device says, and the local
    # directories searched first hold none — otherwise the foreign host
    # would never be asked, which is the tiering working as it should.
    empty = Source(tmp_path / "empty")
    opened_for: list[str] = []

    class _Elsewhere:
        """The one method a resolution asks of a registry client."""

        def index(self, name: str):
            return SimpleNamespace(
                entries=json.loads((foreign.path / "index.json").read_text(encoding="utf-8"))[
                    "packages"
                ],
                base=str(foreign.path),
                source=name,
                url_for=lambda entry: "",
            )

        def fetch_meta(self, index, meta):
            del index
            return (foreign.path / meta.file).read_bytes()

    def hosts(domain: str):
        opened_for.append(domain)
        return _Elsewhere()

    lines: list[str] = []
    pin = resolved(
        source,
        tmp_path,
        workspace=f"packages.example.test/build-workspace/{WORKSPACE}",
        workspace_sources=(empty.path,),
        hosts=hosts,
        on_line=lines.append,
    )
    assert opened_for == ["packages.example.test"]
    assert pin.workspace.sha256 == foreign.hash_of(WORKSPACE, "0.1.0")
    # The SDK required this package from its own host, and the device
    # took it from another — worth the one line, and not a refusal.
    assert len(lines) == 1


def test_a_pin_the_index_disagrees_with_is_refused(tmp_path) -> None:
    """Same version, other bytes, is a refusal and never a substitution.

    The guard against a mirror that publishes different bytes under the
    version a context was created against.
    """
    source = chained(tmp_path)
    with pytest.raises(BuildError) as caught:
        concrete_package(
            PackagePin(name=WORKSPACE, version="0.1.0", sha256="ff" * 32),
            source="build-workspace",
            sources=(source.path,),
        )
    assert "pinned to" in caught.value.message


def test_a_source_that_publishes_other_bytes_under_the_pinned_version_is_refused(
    tmp_path,
) -> None:
    """The one thing that is never shopped around for: same version, other bytes."""
    source = chained(tmp_path)
    with pytest.raises(BuildError) as caught:
        concrete_package(
            PackagePin(name=WORKSPACE, version="0.1.0", sha256="ff" * 32),
            source="build-workspace",
            sources=(source.path, source.path),
        )
    assert "pinned to" in caught.value.message


# --------------------------------------------------------------------------
# Through a device file
# --------------------------------------------------------------------------


def _pins_of(model, source: Source, work_root: Path):
    """The two pins a device model resolves to, through the production path.

    The same three model fields ``create_build_context`` hands over, in
    the same order, so what this asserts is what a build would get.
    """
    constraint, prereleases = sdk_constraint(model.sources.sdk)
    found = resolve_sdk((source.path,), constraint=constraint, prereleases=prereleases)
    return (constraint, prereleases), resolve_environment(
        workspace=model.sources.build_workspace,
        tools=model.sources.build_tools,
        sdk_source=model.sources.sdk,
        sdk=found,
        sources=(source.path,),
        workspace_sources=(source.path,),
        tools_sources=(source.path,),
        work_root=work_root,
    )


def test_a_device_without_sources_states_no_pin_and_gets_the_defaults(
    tmp_path, write_config
) -> None:
    """The ordinary device: nothing written down, everything resolved.

    A device file that names no ``sources:`` carries the three default
    references — a package and no constraint — so the SDK resolves
    against the minor this workbench was released alongside and the
    environment against the chain that SDK release states. Nothing is
    written back into the device, which is what keeps it from being
    frozen onto whatever was current on the day it was created.
    """
    source = chained(tmp_path)
    model = resolve_file(write_config(VALID_CONFIG))

    assert (model.sources.sdk, model.sources.build_workspace, model.sources.build_tools) == (
        DEFAULT_SDK,
        DEFAULT_BUILD_WORKSPACE,
        DEFAULT_BUILD_TOOLS,
    )
    assert sdk_constraint(model.sources.sdk) == (DEFAULT_SDK_CONSTRAINT, True)
    _, pin = _pins_of(model, source, tmp_path / "work")
    assert pin.workspace.sha256 == source.hash_of(WORKSPACE, "0.1.0")
    assert pin.tools.sha256 == source.hash_of(TOOLS, "0.1.0")


def test_a_device_file_can_pin_the_sdk(tmp_path, write_config) -> None:
    """``sources.sdk`` in a device file decides the SDK constraint.

    The device names a version, so the default minor does not apply and
    the pre-release rule goes back to the ordinary one — which is
    :func:`sdk_constraint`'s answer, not this test's own arithmetic.
    """
    source = chained(tmp_path)
    model = resolve_file(write_config(VALID_CONFIG + f"\nsources:\n  sdk: {DEFAULT_SDK}:0.1.0\n"))

    assert model.sources.sdk == f"{DEFAULT_SDK}:0.1.0"
    stated, pin = _pins_of(model, source, tmp_path / "work")
    assert stated == ("==0.1.0", None)
    # The other two entries were not stated and still come from the chain.
    assert pin.workspace.sha256 == source.hash_of(WORKSPACE, "0.1.0")
    assert pin.tools.sha256 == source.hash_of(TOOLS, "0.1.0")


@pytest.mark.parametrize(
    ("key", "package"),
    [("build_workspace", WORKSPACE), ("build_tools", TOOLS)],
)
def test_a_device_file_can_pin_one_environment_package(
    tmp_path, write_config, key: str, package: str
) -> None:
    """Each environment override reaches the resolution, and alone.

    The stated package is the device's word — version and hash, so
    nothing is resolved for it — while the other one still comes out of
    the chain. That the two are independent is the property: a device
    that pins one package must not silently pin the other to today's
    version with it.

    The workspace is pinned to the bytes the source really publishes,
    because the stage below it reads what *that* package requires: an
    override replaces one package and not the statement it makes about
    the next one. Pinning the tools needs no such thing — the chain ends
    there.
    """
    source = chained(tmp_path)
    kind = "build-workspace" if key == "build_workspace" else "build-tools"
    digest = source.hash_of(WORKSPACE, "0.1.0") if key == "build_workspace" else "cd" * 32
    reference = f"{kind}/{package}:0.1.0@sha256:{digest}"
    model = resolve_file(write_config(VALID_CONFIG + f"\nsources:\n  {key}: {reference}\n"))

    assert getattr(model.sources, key) == reference
    _, pin = _pins_of(model, source, tmp_path / "work")
    stated, derived = (
        (pin.workspace, pin.tools) if key == "build_workspace" else (pin.tools, pin.workspace)
    )
    other = TOOLS if key == "build_workspace" else WORKSPACE
    assert stated.sha256 == digest
    assert derived.sha256 == source.hash_of(other, "0.1.0")


def test_a_kind_is_not_looked_for_under_another_kinds_key(tmp_path) -> None:
    """The directory that holds the SDK is no claim about the other two.

    One key per package kind, and no fallback between them: a machine
    that keeps everything in one directory names that directory in all
    three keys, which is the statement it is actually making. Stating it
    once used to answer for all three, and a build then resolved a
    workspace out of a directory nobody had offered for one.
    """
    source = chained(tmp_path)
    found = resolve_sdk((source.path,), constraint="==0.1.0", prereleases=True)
    with pytest.raises(BuildError) as caught:
        resolve_environment(
            workspace=DEFAULT_BUILD_WORKSPACE,
            tools=DEFAULT_BUILD_TOOLS,
            sdk_source=DEFAULT_SDK,
            sdk=found,
            sources=(source.path,),  # the SDK's key alone
            work_root=tmp_path / "work",
        )
    assert "build workspace package" in caught.value.message
    # And with its own key it is found again, from the same directory.
    assert resolved(source, tmp_path).workspace.version
