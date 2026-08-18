# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The context directory: :mod:`mcuhome.workbench.contextdir`.

The workbench half of the subject. ``mcuhome-sdk``'s ``test_context.py``
pins the format and the normative ID rule (:mod:`mcuhome.model.context`);
this file pins the directory the rule is applied to — what ``create_context``
writes, what ``lock_context`` freezes, and what ``verify_context`` makes
of a directory that has since been edited. ADR 0020 puts the two in
different packages on purpose: a build server recomputes the ID from
bytes off a socket and carries no build logic at all.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, resolve_file
from mcuhome.model.context import (
    BACKEND_DIR,
    CONTEXT_FILE,
    CONTEXT_VERSION,
    MANIFEST_FILE,
    MODEL_FILE,
    SIGNING_KEY_FILE,
    ContextManifest,
    ContextRequest,
    EnvironmentPin,
    SdkPin,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.model import DeviceModel
from ruamel.yaml import YAML

from mcuhome.workbench.contextdir import (
    context_facts,
    create_context,
    lock_context,
    read_context_manifest,
    read_context_request,
    verify_context,
    write_context_manifest,
)
from mcuhome.workbench.signing import generate_key_pem, public_key_pem

EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"

# The fixed synthetic inputs, spelled the same way mcuhome-sdk's
# test_context.py spells
# them — duplicated rather than imported, because the two files are two
# repositories after ADR 0024.
DIGEST = "sha256:" + "ab" * 32
SDK_SHA = "cd" * 32

#: Resolved pins for the create tests. The constraint is PEP 440 (ADR
#: 0018's amendment, E52). The URL uses a reserved domain (RFC 2606): it
#: is advisory data, and no test ever fetches it.
SDK = SdkPin(
    constraint="~=0.1.0",
    version="0.1.0",
    url="https://example.invalid/mcuhome-sdk-0.1.0.tar.zst",
    sha256=SDK_SHA,
)
#: The pinned build environment for the context. Written at the lock,
#: recorded in ``manifest.yaml``, and part of the identity — hashing the
#: digest alone, not the reference, so the same image fetched from a
#: mirror is the same build.
ENVIRONMENT = EnvironmentPin(reference="ghcr.io/mcu-home/build-container:zephyr-4.4.0-r1@" + DIGEST)

#: The request timestamp, an explicit argument so two creations of the
#: same inputs are byte-identical (ADR 0018: created is the one field
#: allowed to differ, and only because the caller supplies it).
CREATED = datetime(2026, 8, 10, 9, 0, 0, tzinfo=UTC)

#: A fixed key pair so the public half is a constant — the context bytes
#: have to be reproducible, which a fresh random key would break. The
#: private half is kept around only to prove create_context refuses it.
_PRIVATE_PEM = generate_key_pem(scalar=0x1234ABCD)
SIGNING_PUB = public_key_pem(_PRIVATE_PEM)


@pytest.fixture(scope="module")
def model() -> DeviceModel:
    return resolve_file(EXAMPLE)


def _create(model: DeviceModel, out_dir: Path, **overrides) -> ContextRequest:
    arguments = {
        "sdk": SDK,
        "build_environment": ENVIRONMENT,
        "signing_pub": SIGNING_PUB,
        "created": CREATED,
    }
    arguments.update(overrides)
    return create_context(model, out_dir=out_dir, **arguments)


def _lock(model: DeviceModel, out_dir: Path, **overrides) -> ContextManifest:
    """Create a base context and freeze it — what a local build method does.

    ``create_context`` writes only the request; the ``files`` list and the
    ID exist only once the context is locked, so every test that needs a
    ``manifest.yaml`` goes through here rather than through create alone.
    """
    _create(model, out_dir, **overrides)
    return lock_context(out_dir)


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
# The container resolution, as the manifest records it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    [
        "ghcr.io/mcu-home/build-container:zephyr-4.4.0-r1",  # missing digest
        "ghcr.io/mcu-home/build-container:zephyr-4.4.0-r1@ab" + "ab" * 31,  # no algorithm prefix
        "ghcr.io/mcu-home/build-container:zephyr-4.4.0-r1@sha256:" + "AB" * 32,  # uppercase
        "ghcr.io/mcu-home/build-container:zephyr-4.4.0-r1@sha256:" + "ab" * 16,  # wrong length
        "ghcr.io/mcu-home/build-container:zephyr-4.4.0-r1@sha512:" + "ab" * 32,  # not sha256
    ],
)
def test_a_malformed_build_environment_reference_is_refused(
    model, tmp_path: Path, reference: str
) -> None:
    """The reference must carry a valid sha256 digest for the context ID.

    Format 3 hashes the digest, so the pin must have exactly one spelling.
    """
    pin = EnvironmentPin(reference=reference)
    with pytest.raises(BuildError):
        # The digest property checks the reference strictly
        _ = pin.digest


# --------------------------------------------------------------------------
# What is excluded stays excluded
# --------------------------------------------------------------------------


def test_the_informational_fields_do_not_influence_the_id(model, tmp_path: Path) -> None:
    """created, constraint, version, url are informational."""
    manifest = _lock(model, tmp_path / "context")
    variants = [
        replace(manifest, sdk=replace(SDK, constraint="~9.9.9")),
        # The version and the URL are names for bytes the sha256 pins.
        replace(manifest, sdk=replace(SDK, version="9.9.9")),
        replace(manifest, sdk=replace(SDK, url="file:///srv/mirror/sdk.tar.zst")),
    ]
    assert {variant.compute_id() for variant in variants} == {manifest.id}


def test_the_manifest_carries_no_timestamp(model, tmp_path: Path) -> None:
    """ "The one field that does not travel is `created`" (ADR 0018).

    It dates the *request* and lives in context.yaml alone; the
    manifest's own moment is the lock. So two creations of the same
    inputs yield byte-identical manifests with no argument saying so —
    and a manifest that carries a stray ``created`` anyway is read under
    the unknown-field rule: ignored, not refused.
    """
    manifest = _lock(model, tmp_path / "one")
    assert "created" not in manifest.to_dict()
    assert manifest == ContextManifest.from_dict(
        {**manifest.to_dict(), "created": "1999-01-01T00:00:00Z"}
    )


def test_yaml_formatting_is_irrelevant_to_the_id(model, tmp_path: Path) -> None:
    """The ID hashes values, never the manifest's bytes."""
    out_dir = tmp_path / "context"
    manifest = _lock(model, out_dir)
    reordered = (
        "# a comment, and every section in a different order\n"
        f"id: {manifest.id}\n"
        f"target: {{board: {manifest.board}}}\n"
        "files:\n"
        + "".join(f"- {{sha256: {entry.sha256}, path: {entry.path}}}\n" for entry in manifest.files)
        + f"build_environment: {manifest.build_environment.reference}\n"
        "mcuhome:\n"
        f"  package: {{sha256: {SDK.sha256}, url: {SDK.url}}}\n"
        f"  version: {SDK.version}\n"
        f"  constraint: '{SDK.constraint}'\n"
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


def test_the_signing_key_lands_in_the_context(model, tmp_path: Path) -> None:
    """keys/signing.pub is context content (ADR 0018 amendment)."""
    out_dir = tmp_path / "context"
    _create(model, out_dir)
    assert (out_dir / SIGNING_KEY_FILE).read_text(encoding="utf-8") == SIGNING_PUB


def test_the_locked_manifest_lists_every_content_file_but_neither_document(
    model, tmp_path: Path
) -> None:
    """The freeze lists model and key, and never either context document."""
    out_dir = tmp_path / "context"
    manifest = _lock(model, out_dir)
    assert [entry.path for entry in manifest.files] == [SIGNING_KEY_FILE, MODEL_FILE]
    listed = {entry.path for entry in manifest.files}
    assert MANIFEST_FILE not in listed
    assert CONTEXT_FILE not in listed


def test_the_declared_id_is_the_recomputed_id(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    manifest = _lock(model, out_dir)
    assert manifest.id == manifest.compute_id()
    assert verify_context(out_dir).ok


def test_the_manifest_round_trips_through_yaml(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    manifest = _lock(model, out_dir)
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
# The request document — context.yaml
# --------------------------------------------------------------------------


def test_the_request_carries_pins_and_created_but_no_files_or_id(model, tmp_path: Path) -> None:
    """context.yaml is the request: pins + intent + created, and nothing the freeze makes."""
    _create(model, tmp_path / "context")
    document = YAML(typ="safe").load(
        (tmp_path / "context" / CONTEXT_FILE).read_text(encoding="utf-8")
    )
    assert document["context"] == CONTEXT_VERSION
    assert document["created"] == "2026-08-10T09:00:00Z"
    # Intent and resolution stand side by side (ADR 0018 decision 3).
    assert document["mcuhome"]["constraint"] == SDK.constraint
    assert document["mcuhome"]["version"] == SDK.version
    assert document["mcuhome"]["package"] == {"url": SDK.url, "sha256": SDK.sha256}
    # The pinned build environment, resolved by the client before the
    # request (E61, Format 3: the client resolves the environment).
    assert document["build_environment"] == ENVIRONMENT.reference
    assert document["target"] == {"board": model.device.board}
    # The freeze's outputs cannot exist yet: no integrity list, no identity.
    assert "files" not in document
    assert "id" not in document


def test_the_request_round_trips_through_a_yaml_load(model, tmp_path: Path) -> None:
    """read_context_request reads back exactly the request create_context wrote."""
    out_dir = tmp_path / "context"
    request = _create(model, out_dir)
    assert read_context_request(out_dir / CONTEXT_FILE) == request


def test_two_creations_of_the_same_inputs_are_byte_identical(model, tmp_path: Path) -> None:
    """No clock leak: identical inputs (created included) yield identical bytes."""
    first, second = tmp_path / "a", tmp_path / "b"
    _create(model, first)
    _create(model, second)
    for name in (CONTEXT_FILE, MODEL_FILE, SIGNING_KEY_FILE):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_a_private_key_in_place_of_the_public_one_is_refused(model, tmp_path: Path) -> None:
    """The private half must never reach a build (ADR 0015 decision 8)."""
    with pytest.raises(BuildError) as caught:
        _create(model, tmp_path / "context", signing_pub=_PRIVATE_PEM)
    assert "public key" in caught.value.message


# --------------------------------------------------------------------------
# Patches are ordinary files
# --------------------------------------------------------------------------


def test_patches_pass_through_as_ordinary_integrity_entries(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    manifest = _lock(model, out_dir, patches_dir=_patches_source(tmp_path))
    assert [entry.path for entry in manifest.files] == [
        SIGNING_KEY_FILE,
        MODEL_FILE,
        "patches/sdk/0001-tweak.patch",
        "patches/zephyr/0001-fix-uart.patch",
        "patches/zephyr/0002-fix-spi.patch",
    ]
    patch = next(entry for entry in manifest.files if entry.path.endswith("0001-fix-uart.patch"))
    assert patch.sha256 == hashlib.sha256(b"--- a\n+++ b\n").hexdigest()
    assert verify_context(out_dir).ok


def test_the_facts_of_a_context_name_its_pins_and_its_patches(model, tmp_path: Path) -> None:
    """What a build says about the context it just wrote (PO 2026-08-16).

    Read back off the directory, so what a person is shown is what the
    build environment receives — including *which* patches ride along,
    the question a patched build environment actually raises.
    """
    out_dir = tmp_path / "context"
    manifest = _lock(model, out_dir, patches_dir=_patches_source(tmp_path))
    facts = context_facts(out_dir)
    assert facts["sdk"] == SDK.version
    assert facts["sdk_sha256"] == SDK.sha256
    assert facts["build_environment"] == ENVIRONMENT.reference
    assert facts["board"] == model.device.board
    assert facts["files"] == len(manifest.files)
    assert facts["id"] == manifest.id
    assert facts["patches"] == [
        "sdk/0001-tweak.patch",
        "zephyr/0001-fix-uart.patch",
        "zephyr/0002-fix-spi.patch",
    ]


def test_a_base_context_has_facts_but_no_identity_yet(model, tmp_path: Path) -> None:
    """Freezing is the locking party's act, so an unlocked context has no ID."""
    out_dir = tmp_path / "context"
    _create(model, out_dir)
    facts = context_facts(out_dir)
    assert "id" not in facts
    assert facts["sdk"] == SDK.version
    assert facts["patches"] == []
    assert facts["files"] == 2  # the model and the public signing key


def test_patches_change_the_id_like_any_other_file(model, tmp_path: Path) -> None:
    plain = _lock(model, tmp_path / "plain")
    patched = _lock(model, tmp_path / "patched", patches_dir=_patches_source(tmp_path))
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
    _lock(model, out_dir, patches_dir=_patches_source(tmp_path))
    report = verify_context(out_dir)
    assert report.ok
    assert report.mismatches == ()
    assert report.declared_id == report.actual_id
    assert report.problems() == []


def test_a_tampered_file_is_detected(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    manifest = _lock(model, out_dir, patches_dir=_patches_source(tmp_path))
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
    manifest = _lock(model, out_dir)
    _rewrite_manifest(out_dir, id="sha256:" + "00" * 32)
    report = verify_context(out_dir)
    assert not report.ok
    assert report.mismatches == ()
    assert report.actual_id == manifest.id  # the bytes still hash to the truth
    assert report.declared_id == "sha256:" + "00" * 32
    assert any("context id" in problem for problem in report.problems())


def test_a_listed_but_missing_file_is_detected(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    _lock(model, out_dir, patches_dir=_patches_source(tmp_path))
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
    manifest = _lock(model, out_dir)
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
    manifest = _lock(model, out_dir)
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
    _lock(model, out_dir)
    path = out_dir / MANIFEST_FILE
    text = path.read_text(encoding="utf-8").replace(f"context: {CONTEXT_VERSION}", "context: 99")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        verify_context(out_dir)
    assert "99" in caught.value.message
    assert str(CONTEXT_VERSION) in caught.value.message


def test_a_manifest_that_is_not_yaml_is_a_refusal(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    _lock(model, out_dir)
    (out_dir / MANIFEST_FILE).write_text("files: [unclosed\n", encoding="utf-8")
    with pytest.raises(BuildError) as caught:
        verify_context(out_dir)
    assert "not valid YAML" in caught.value.message


def test_a_manifest_missing_a_section_is_a_refusal(model, tmp_path: Path) -> None:
    out_dir = tmp_path / "context"
    _lock(model, out_dir)
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
    _lock(model, out_dir)
    _rewrite_manifest(out_dir, sdk=replace(SDK, sha256=SDK_SHA.upper()))
    with pytest.raises(BuildError) as caught:
        verify_context(out_dir)
    assert "SDK package hash" in caught.value.message


# --------------------------------------------------------------------------
# Context format 2: the requirement, and the backend's answer to it (E61)
# --------------------------------------------------------------------------


def test_the_manifest_carries_the_pinned_environment(model, tmp_path: Path) -> None:
    """The client pins the environment; the manifest records the pin (Format 3).

    The request carries the client's resolution; the manifest restates it.
    """
    out_dir = tmp_path / "context"
    manifest = _lock(model, out_dir)
    assert manifest.build_environment == ENVIRONMENT
    document = YAML(typ="safe").load((out_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert document["build_environment"] == ENVIRONMENT.reference


def test_no_hash_in_the_manifest_is_wrapped_across_two_lines(model, tmp_path: Path) -> None:
    """A ``sha256:`` digest is 71 characters and the emitter would fold it.

    Legal YAML — and this module's own round-trip test never noticed,
    because ruamel folds it straight back. It is still the wrong thing to
    write: ``manifest.yaml`` is read by build containers this project
    does not write, in languages it does not choose, and §3.3.1 has them
    **refuse** a hash rendered any other way rather than repair it. A
    one-line value cannot be read as two.
    """
    out_dir = tmp_path / "context"
    manifest = _lock(model, out_dir)
    text = (out_dir / MANIFEST_FILE).read_text(encoding="utf-8")
    assert f"build_environment: {ENVIRONMENT.reference}" in text
    assert f"id: {manifest.id}" in text
    assert f"sha256: {SDK.sha256}" in text
    for entry in manifest.files:
        assert f"sha256: {entry.sha256}" in text
