# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build context and its normative ID.

Covers both halves: :mod:`mcuhome.context` is the format and the ID rule,
:mod:`mcuhome.contextdir` is the directory the rule is applied to (ADR
0020 puts them in different packages — a build server recomputes the ID
from bytes off a socket and carries no build logic).

The context ID rule (build-container-contract.md §3.3, ADR 0018) is
locked with ``context`` format version 1 and can never change
afterwards — every archived context, every artifact attribution and
every server-side integrity check depends on the same inputs hashing to
the same ID forever. That makes :data:`GOLDEN_ID` the contract of this
file, not a regression convenience: if it ever fails, the fix is in the
code, never in the constant.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, resolve_file

from mcuhome.context import (
    BACKEND_DIR,
    CONTEXT_ID_VECTORS,
    CONTEXT_VERSION,
    MANIFEST_FILE,
    MODEL_FILE,
    ContainerPin,
    ContextFile,
    ContextManifest,
    SdkPin,
    canonical_json,
    context_id,
    vector_id,
)
from mcuhome.contextdir import (
    create_context,
    read_context_manifest,
    verify_context,
    write_context_manifest,
)
from mcuhome.errors import BuildError
from mcuhome.model import DeviceModel

EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"

# The fixed synthetic inputs of the golden vector.
DIGEST = "sha256:" + "ab" * 32
SDK_SHA = "cd" * 32
BOARD = "nrf7002dk/nrf5340/cpuapp"
FILES = (
    ContextFile(path="model/device-model.json", sha256="11" * 32),
    ContextFile(path="patches/zephyr/0001-fix.patch", sha256="22" * 32),
)

#: The golden vector: the inputs above hash to exactly this ID, in this
#: builder and in every builder that will ever exist. NEVER update this
#: constant — a change here is a change to a frozen contract, and the
#: bug is in the code that made it necessary.
GOLDEN_ID = "sha256:dde9df3b7ab59f8ad8197b6916f437ed3502ce88275b48f5e122b89e48b99c3f"

#: Resolved pins for the create tests. The URL uses a reserved domain
#: (RFC 2606): it is advisory data, and no test ever fetches it.
SDK = SdkPin(
    constraint="^0.1.0",
    version="0.1.0",
    url="https://example.invalid/mcuhome-sdk-0.1.0.tar.zst",
    sha256=SDK_SHA,
)
CONTAINER = ContainerPin(image="ghcr.io/mcu-home/builder", tag="zephyr-4.4.0-r1", digest=DIGEST)


@pytest.fixture(scope="module")
def model() -> DeviceModel:
    return resolve_file(EXAMPLE)


def _create(model: DeviceModel, out_dir: Path, **overrides) -> ContextManifest:
    arguments = {"sdk": SDK, "container": CONTAINER, "created": "2026-08-08T10:00:00Z"}
    arguments.update(overrides)
    return create_context(model, out_dir=out_dir, **arguments)


def _patches_source(tmp_path: Path) -> Path:
    source = tmp_path / "patches-src"
    (source / "zephyr").mkdir(parents=True)
    (source / "zephyr" / "0001-fix-uart.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
    (source / "zephyr" / "0002-fix-spi.patch").write_text("--- c\n+++ d\n", encoding="utf-8")
    (source / "sdk").mkdir()
    (source / "sdk" / "0001-tweak.patch").write_text("--- e\n+++ f\n", encoding="utf-8")
    return source


def _rewrite_manifest(out_dir: Path, **overrides) -> ContextManifest:
    """Write the context's manifest back with some fields replaced."""
    manifest = replace(read_context_manifest(out_dir / MANIFEST_FILE), **overrides)
    write_context_manifest(manifest, out_dir=out_dir)
    return manifest


# --------------------------------------------------------------------------
# Canonical JSON — the encoding under the hash
# --------------------------------------------------------------------------


def test_canonical_json_sorts_keys_and_uses_minimal_separators() -> None:
    value = {"b": "1", "a": {"d": "2", "c": "3"}, "list": ["x", "y"]}
    assert canonical_json(value) == '{"a":{"c":"3","d":"2"},"b":"1","list":["x","y"]}'


def test_canonical_json_emits_non_ascii_literally() -> None:
    """RFC 8785 forbids \\u escapes for characters that need none."""
    assert canonical_json({"board": "nrf–ü"}) == '{"board":"nrf–ü"}'
    assert canonical_json({"a": "ü"}).encode("utf-8") == b'{"a":"\xc3\xbc"}'


def test_canonical_json_escapes_strings_the_ecmascript_way() -> None:
    """Two-character escapes where they exist, lowercase \\u00xx otherwise."""
    assert canonical_json({"a": 'x"\\\n\t\x1f'}) == '{"a":"x\\"\\\\\\n\\t\\u001f"}'


# --------------------------------------------------------------------------
# The context ID — the normative rule, frozen
# --------------------------------------------------------------------------


def test_the_golden_vector_never_changes() -> None:
    """The regression anchor of the whole format. See GOLDEN_ID."""
    computed = context_id(container_digest=DIGEST, sdk_sha256=SDK_SHA, board=BOARD, files=FILES)
    assert computed == GOLDEN_ID


@pytest.mark.parametrize("vector", CONTEXT_ID_VECTORS, ids=lambda vector: vector["name"])
def test_the_conformance_vectors_hold(vector) -> None:
    """The suite a *second* implementation is checked against.

    The golden vector above proves this builder does not drift. It cannot
    prove anything about the build server, which ADR 0019 §8 obliges to
    recompute the same ID from the bytes it received — and which, per ADR
    0020 decision 4, is entitled to do so with nothing but the model
    package. :data:`CONTEXT_ID_VECTORS` ships inside that package so the
    obligation is checkable rather than asserted, and this test is the
    Python side running it.

    A failure here is never fixed in the data: version 1's vectors are
    frozen exactly as :data:`GOLDEN_ID` is.
    """
    assert vector_id(vector) == vector["id"]


def test_the_golden_vector_is_one_of_the_conformance_vectors() -> None:
    """Two frozen constants that disagree would be worse than one.

    The vector this file has pinned since the format was written is in
    the package's own suite, so a second implementation is checked
    against the same value the test suite is — not a second one that
    happens to look like it.
    """
    assert GOLDEN_ID in {vector["id"] for vector in CONTEXT_ID_VECTORS}


def test_the_golden_vectors_canonical_form_never_changes() -> None:
    """The exact bytes under the hash, spelled out — nesting, order, all."""
    expected = (
        '{"container":{"digest":"' + DIGEST + '"},'
        '"files":['
        '{"path":"model/device-model.json","sha256":"' + "11" * 32 + '"},'
        '{"path":"patches/zephyr/0001-fix.patch","sha256":"' + "22" * 32 + '"}],'
        '"sdk":{"sha256":"' + SDK_SHA + '"},'
        '"target":{"board":"' + BOARD + '"}}'
    )
    assert "sha256:" + hashlib.sha256(expected.encode("utf-8")).hexdigest() == GOLDEN_ID


def test_the_order_files_are_given_in_does_not_matter() -> None:
    """The sort is part of the rule: the list is a set with an encoding."""
    computed = context_id(
        container_digest=DIGEST, sdk_sha256=SDK_SHA, board=BOARD, files=reversed(FILES)
    )
    assert computed == GOLDEN_ID


def test_every_hashed_field_changes_the_id() -> None:
    variants = [
        {"container_digest": "sha256:" + "ba" * 32},
        {"sdk_sha256": "dc" * 32},
        {"board": "nrf52840dk/nrf52840"},
        # A file's content, a file's path, one file more, one file less.
        {"files": (FILES[0], replace(FILES[1], sha256="33" * 32))},
        {"files": (FILES[0], replace(FILES[1], path="patches/sdk/0001-fix.patch"))},
        {"files": (*FILES, ContextFile(path="patches/sdk/0001-more.patch", sha256="44" * 32))},
        {"files": FILES[:1]},
    ]
    ids = {
        context_id(
            **{
                "container_digest": DIGEST,
                "sdk_sha256": SDK_SHA,
                "board": BOARD,
                "files": FILES,
                **variant,
            }
        )
        for variant in variants
    }
    assert GOLDEN_ID not in ids
    assert len(ids) == len(variants), "two different inputs collided on one ID"


def test_a_duplicate_path_is_refused() -> None:
    with pytest.raises(BuildError) as caught:
        context_id(
            container_digest=DIGEST,
            sdk_sha256=SDK_SHA,
            board=BOARD,
            files=(FILES[0], replace(FILES[0], sha256="33" * 32)),
        )
    assert "twice" in caught.value.message


@pytest.mark.parametrize(
    "digest",
    [
        "ab" * 32,  # no algorithm prefix
        "sha256:" + "AB" * 32,  # uppercase: a second spelling of the same hash
        "sha256:" + "ab" * 16,  # wrong length
        "sha512:" + "ab" * 32,  # not the version-1 algorithm
    ],
)
def test_a_malformed_container_digest_is_refused(digest: str) -> None:
    with pytest.raises(BuildError):
        context_id(container_digest=digest, sdk_sha256=SDK_SHA, board=BOARD, files=FILES)


def test_a_malformed_file_hash_is_refused() -> None:
    with pytest.raises(BuildError):
        context_id(
            container_digest=DIGEST,
            sdk_sha256=SDK_SHA,
            board=BOARD,
            files=(replace(FILES[0], sha256="not-a-hash"),),
        )


def test_a_missing_board_is_refused() -> None:
    with pytest.raises(BuildError):
        context_id(container_digest=DIGEST, sdk_sha256=SDK_SHA, board="  ", files=FILES)


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "patches/../escape.patch", "patches\\zephyr\\0001.patch", "", "a//b"],
)
def test_an_unusable_path_is_refused(path: str) -> None:
    with pytest.raises(BuildError):
        context_id(
            container_digest=DIGEST,
            sdk_sha256=SDK_SHA,
            board=BOARD,
            files=(ContextFile(path=path, sha256="11" * 32),),
        )


@pytest.mark.parametrize("path", [MANIFEST_FILE, f"{BACKEND_DIR}/command.json"])
def test_the_integrity_list_may_not_name_what_is_not_content(path: str) -> None:
    """The manifest and the backend's runtime directory stay out of the ID."""
    with pytest.raises(BuildError) as caught:
        context_id(
            container_digest=DIGEST,
            sdk_sha256=SDK_SHA,
            board=BOARD,
            files=(ContextFile(path=path, sha256="11" * 32),),
        )
    assert "must not name" in caught.value.message


# --------------------------------------------------------------------------
# What is excluded stays excluded
# --------------------------------------------------------------------------


def test_the_informational_fields_do_not_influence_the_id(model, tmp_path: Path) -> None:
    """created, constraint, version, url, image, tag — advisory, all of them."""
    manifest = _create(model, tmp_path / "context")
    variants = [
        replace(manifest, created="1999-01-01T00:00:00Z"),
        replace(manifest, sdk=replace(SDK, constraint="~9.9.9")),
        # The version and the URL are names for bytes the sha256 pins.
        replace(manifest, sdk=replace(SDK, version="9.9.9")),
        replace(manifest, sdk=replace(SDK, url="file:///srv/mirror/sdk.tar.zst")),
        # A context resolved via `latest` and one resolved via the
        # equivalent versioned tag hash identically.
        replace(manifest, container=replace(CONTAINER, image="mirror.example/builder")),
        replace(manifest, container=replace(CONTAINER, tag="latest")),
    ]
    assert {variant.compute_id() for variant in variants} == {manifest.id}


def test_two_creations_at_different_times_share_one_id(model, tmp_path: Path) -> None:
    first = _create(model, tmp_path / "one", created="2026-08-08T10:00:00Z")
    second = _create(model, tmp_path / "two", created="2027-01-01T00:00:00Z")
    assert first.created != second.created
    assert first.id == second.id


def test_yaml_formatting_is_irrelevant_to_the_id(model, tmp_path: Path) -> None:
    """The ID hashes values, never the manifest's bytes."""
    out_dir = tmp_path / "context"
    manifest = _create(model, out_dir)
    reordered = (
        "# a comment, and every section in a different order\n"
        f"id: {manifest.id}\n"
        f"target: {{board: {manifest.board}}}\n"
        "files:\n"
        + "".join(f"- {{sha256: {entry.sha256}, path: {entry.path}}}\n" for entry in manifest.files)
        + f"container: {{digest: {CONTAINER.digest}, tag: {CONTAINER.tag}, "
        f"image: {CONTAINER.image}}}\n"
        "mcuhome:\n"
        f"  package: {{sha256: {SDK.sha256}, url: {SDK.url}}}\n"
        f"  version: {SDK.version}\n"
        f"  constraint: '{SDK.constraint}'\n"
        f"created: '{manifest.created}'\n"
        f"context: {CONTEXT_VERSION}\n"
    )
    (out_dir / MANIFEST_FILE).write_text(reordered, encoding="utf-8")
    read_back = read_context_manifest(out_dir / MANIFEST_FILE)
    assert read_back.compute_id() == manifest.id
    assert verify_context(out_dir).ok


# --------------------------------------------------------------------------
# Creating a context
# --------------------------------------------------------------------------


def test_a_created_context_carries_the_model_verbatim(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    _create(model, out_dir)
    assert (out_dir / MODEL_FILE).read_text(encoding="utf-8") == model.to_json()


def test_the_manifest_lists_every_file_but_never_itself(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    manifest = _create(model, out_dir)
    assert [entry.path for entry in manifest.files] == [MODEL_FILE]


def test_the_board_and_the_pins_are_recorded(model, tmp_path: Path) -> None:
    manifest = _create(model, tmp_path / "context")
    assert manifest.board == model.device.board
    assert manifest.sdk == SDK
    assert manifest.container == CONTAINER
    assert manifest.context_version == CONTEXT_VERSION


def test_the_declared_id_is_the_recomputed_id(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    manifest = _create(model, out_dir)
    assert manifest.id == manifest.compute_id()
    assert verify_context(out_dir).ok


def test_the_manifest_round_trips_through_yaml(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    manifest = _create(model, out_dir)
    assert read_context_manifest(out_dir / MANIFEST_FILE) == manifest


def test_a_non_empty_target_directory_is_refused(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    out_dir.mkdir()
    (out_dir / "leftover.txt").write_text("stale\n", encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        _create(model, out_dir)
    assert "already contains files" in caught.value.message


def test_a_target_that_is_a_file_is_refused(model, tmp_path: Path) -> None:
    target = tmp_path / "context"
    target.write_text("", encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        _create(model, target)
    assert "not a directory" in caught.value.message


# --------------------------------------------------------------------------
# Patches are ordinary files
# --------------------------------------------------------------------------


def test_patches_pass_through_as_ordinary_integrity_entries(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    manifest = _create(model, out_dir, patches_dir=_patches_source(tmp_path))
    assert [entry.path for entry in manifest.files] == [
        MODEL_FILE,
        "patches/sdk/0001-tweak.patch",
        "patches/zephyr/0001-fix-uart.patch",
        "patches/zephyr/0002-fix-spi.patch",
    ]
    patch = next(entry for entry in manifest.files if entry.path.endswith("0001-fix-uart.patch"))
    assert patch.sha256 == hashlib.sha256(b"--- a\n+++ b\n").hexdigest()
    assert verify_context(out_dir).ok


def test_patches_change_the_id_like_any_other_file(model, tmp_path: Path) -> None:
    plain = _create(model, tmp_path / "plain")
    patched = _create(model, tmp_path / "patched", patches_dir=_patches_source(tmp_path))
    assert plain.id != patched.id


def test_a_file_at_the_top_of_the_patches_directory_is_refused(model, tmp_path: Path) -> None:
    source = _patches_source(tmp_path)
    (source / "0001-floating.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        _create(model, tmp_path / "context", patches_dir=source)
    assert "not a patch layer" in caught.value.message


def test_a_bad_layer_name_is_refused(model, tmp_path: Path) -> None:
    source = _patches_source(tmp_path)
    (source / "Zephyr").mkdir()
    with pytest.raises(BuildError) as caught:
        _create(model, tmp_path / "context", patches_dir=source)
    assert "layer name" in caught.value.message


def test_a_patch_without_an_order_prefix_is_refused(model, tmp_path: Path) -> None:
    source = _patches_source(tmp_path)
    (source / "zephyr" / "fix-uart.patch").write_text("--- a\n+++ b\n", encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        _create(model, tmp_path / "context", patches_dir=source)
    assert "order" in caught.value.message


def test_a_directory_inside_a_layer_is_refused(model, tmp_path: Path) -> None:
    source = _patches_source(tmp_path)
    (source / "zephyr" / "nested").mkdir()
    with pytest.raises(BuildError) as caught:
        _create(model, tmp_path / "context", patches_dir=source)
    assert "not a patch file" in caught.value.message


def test_a_missing_patches_directory_is_refused(model, tmp_path: Path) -> None:
    with pytest.raises(BuildError) as caught:
        _create(model, tmp_path / "context", patches_dir=tmp_path / "no-such-dir")
    assert "does not exist" in caught.value.message


# --------------------------------------------------------------------------
# Verification — declared values are advisory
# --------------------------------------------------------------------------


def test_a_pristine_context_verifies_clean(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    _create(model, out_dir, patches_dir=_patches_source(tmp_path))
    report = verify_context(out_dir)
    assert report.ok
    assert report.mismatches == ()
    assert report.declared_id == report.actual_id
    assert report.problems() == []


def test_a_tampered_file_is_detected(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    manifest = _create(model, out_dir, patches_dir=_patches_source(tmp_path))
    victim = out_dir / "patches" / "zephyr" / "0001-fix-uart.patch"
    victim.write_text("--- a\n+++ EVIL\n", encoding="utf-8")
    report = verify_context(out_dir)
    assert not report.ok
    [mismatch] = report.mismatches
    assert mismatch.path == "patches/zephyr/0001-fix-uart.patch"
    assert mismatch.declared_sha256 != mismatch.actual_sha256
    assert mismatch.actual_sha256 == hashlib.sha256(b"--- a\n+++ EVIL\n").hexdigest()
    # The context as it actually is has a different identity.
    assert report.actual_id != manifest.id


def test_a_spoofed_id_is_detected(model, tmp_path: Path) -> None:
    """Every file hash matches, only the declared ID lies."""
    out_dir = tmp_path / "context"
    manifest = _create(model, out_dir)
    _rewrite_manifest(out_dir, id="sha256:" + "00" * 32)
    report = verify_context(out_dir)
    assert not report.ok
    assert report.mismatches == ()
    assert report.actual_id == manifest.id  # the bytes still hash to the truth
    assert report.declared_id == "sha256:" + "00" * 32
    assert any("context id" in problem for problem in report.problems())


def test_a_listed_but_missing_file_is_detected(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    _create(model, out_dir, patches_dir=_patches_source(tmp_path))
    (out_dir / "patches" / "sdk" / "0001-tweak.patch").unlink()
    report = verify_context(out_dir)
    assert not report.ok
    [mismatch] = report.mismatches
    assert mismatch.path == "patches/sdk/0001-tweak.patch"
    assert mismatch.actual_sha256 is None
    assert "missing" in mismatch.describe()


def test_a_smuggled_file_is_detected(model, tmp_path: Path) -> None:
    """A file the integrity list does not cover is a finding, not a bonus."""
    out_dir = tmp_path / "context"
    manifest = _create(model, out_dir)
    (out_dir / "patches" / "zephyr").mkdir(parents=True)
    smuggled = out_dir / "patches" / "zephyr" / "0001-smuggled.patch"
    smuggled.write_text("--- a\n+++ b\n", encoding="utf-8")
    report = verify_context(out_dir)
    assert not report.ok
    [mismatch] = report.mismatches
    assert mismatch.path == "patches/zephyr/0001-smuggled.patch"
    assert mismatch.declared_sha256 is None
    # The effective context (the files actually present) has its own ID.
    assert report.actual_id != manifest.id


def test_the_backend_directory_is_not_part_of_the_identity(model, tmp_path: Path) -> None:
    """A mounted context gains .mcuhome/command.json without changing ID."""
    out_dir = tmp_path / "context"
    manifest = _create(model, out_dir)
    (out_dir / BACKEND_DIR).mkdir()
    (out_dir / BACKEND_DIR / "command.json").write_text("{}\n", encoding="utf-8")
    report = verify_context(out_dir)
    assert report.ok
    assert report.actual_id == manifest.id


def test_a_context_without_a_manifest_is_a_refusal(tmp_path: Path) -> None:
    with pytest.raises(BuildError) as caught:
        verify_context(tmp_path)
    assert MANIFEST_FILE in caught.value.message


def test_a_wrong_format_version_is_a_refusal(model, tmp_path: Path) -> None:
    """Named on both sides, never silently coerced."""
    out_dir = tmp_path / "context"
    _create(model, out_dir)
    path = out_dir / MANIFEST_FILE
    text = path.read_text(encoding="utf-8").replace(f"context: {CONTEXT_VERSION}", "context: 99")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        verify_context(out_dir)
    assert "99" in caught.value.message
    assert str(CONTEXT_VERSION) in caught.value.message


def test_a_manifest_that_is_not_yaml_is_a_refusal(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    _create(model, out_dir)
    (out_dir / MANIFEST_FILE).write_text("files: [unclosed\n", encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        verify_context(out_dir)
    assert "not valid YAML" in caught.value.message


def test_a_manifest_missing_a_section_is_a_refusal(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    _create(model, out_dir)
    path = out_dir / MANIFEST_FILE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith(("mcuhome:", "  "))]
    path.write_text("".join(kept), encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        verify_context(out_dir)
    assert "missing something" in caught.value.message


def test_a_manifest_with_a_spoofable_hash_spelling_is_a_refusal(model, tmp_path: Path) -> None:
    """Uppercase hex would give the same bytes a second identity."""
    out_dir = tmp_path / "context"
    _create(model, out_dir)
    _rewrite_manifest(out_dir, sdk=replace(SDK, sha256=SDK_SHA.upper()))
    with pytest.raises(BuildError) as caught:
        verify_context(out_dir)
    assert "SDK package hash" in caught.value.message
