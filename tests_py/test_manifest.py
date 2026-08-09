# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""``build-manifest.json``: what it says, and that it says it the same way.

The manifest is the machine-readable half of a build directory
(builder-pipeline.md §7). Two properties matter enough to pin: it is
deterministic apart from the sizes and hashes it measures, and its
signing block states the arguments the *inline* build would have used —
which is the whole basis of detached signing (ADR 0015 decision 8).

Covers both halves of the document, because the document is one thing:
:mod:`mcuhome.manifest` is its format and :mod:`mcuhome.report` measures
a build directory into it (ADR 0020 puts the two in different packages —
a manifest is read where no toolchain may be installed).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest
from conftest import EXAMPLES_DIR, resolve_file

from mcuhome import __version__, pairing, registry, workspace
from mcuhome import manifest as manifest_module
from mcuhome.errors import BuildError
from mcuhome.generate import APP_DIR, BOOTLOADER_IMAGE
from mcuhome.manifest import MANIFEST_FILE, SigningParameters, read_manifest
from mcuhome.report import (
    SIGN_VERSION_DEFAULT,
    build_manifest,
    signing_parameters,
    write_manifest,
)

EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"
SCHEME = registry.BOARDS["nrf7002dk/nrf5340/cpuapp"].update_scheme

#: What a real build's application .config carries for the two symbols
#: the manifest reads out of it.
APP_KCONFIG = 'CONFIG_ROM_START_OFFSET=0x200\nCONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="0.0.0+0"\n'


def _fake_build(out_dir: Path, *, signed: bool = True) -> tuple[Path, list]:
    """A build directory with the files stage 5 leaves behind."""
    build_dir = out_dir / workspace.BUILD_SUBDIR
    for image, size in ((BOOTLOADER_IMAGE, 512), (APP_DIR, 4096)):
        output = build_dir / image / "zephyr"
        output.mkdir(parents=True)
        (output / "zephyr.elf").write_bytes(b"\x7fELF" + bytes(size))
        (output / "zephyr.hex").write_text(":00000001FF\n", "utf-8")
        (output / "zephyr.bin").write_bytes(bytes(size))
    (build_dir / APP_DIR / "zephyr" / ".config").write_text(APP_KCONFIG, "utf-8")
    if signed:
        (build_dir / APP_DIR / "zephyr" / "zephyr.signed.bin").write_bytes(bytes(4608))
        (build_dir / APP_DIR / "zephyr" / "zephyr.signed.hex").write_text(":00000001FF\n", "utf-8")
    (build_dir / "merged_nrf7002dk_nrf5340_cpuapp.hex").write_text(":00000001FF\n", "utf-8")
    return build_dir, workspace.build_images(build_dir, app_image=APP_DIR)


def _manifest(out_dir: Path, **overrides):
    build_dir, images = _fake_build(out_dir, signed=overrides.pop("signed", True))
    arguments = {
        "out_dir": out_dir,
        "build_dir": build_dir,
        "app_image": APP_DIR,
        "images": images,
        "snippets": ("matter", "boot-mode"),
        "bootloader_snippets": ("boot-mode",),
        "jobs": 2,
        "signed_by_the_build": True,
        "merged": workspace.merged_image(build_dir),
    }
    arguments.update(overrides)
    return build_manifest(resolve_file(EXAMPLE), **arguments)


# --------------------------------------------------------------------------
# The signing parameters
# --------------------------------------------------------------------------


def test_the_signing_parameters_come_from_the_board_layout() -> None:
    """Three of four are registry data — the table the builder itself wrote."""
    parameters = signing_parameters(SCHEME, kconfig={})
    assert parameters.header_size == SCHEME.header_size == 0x200
    assert parameters.align == SCHEME.write_block_size == 4
    assert parameters.slot_size == SCHEME.partition("slot1_partition").size == 912 * 1024
    assert parameters.version == SIGN_VERSION_DEFAULT


def test_a_swap_scheme_sizes_against_the_secondary_slot() -> None:
    """Zephyr's own rule: slot1 for every mode that has one."""
    assert SCHEME.imgtool_slot.label == "slot1_partition"


def test_the_version_is_read_from_the_built_image() -> None:
    kconfig = {"CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION": "1.2.3+4"}
    assert signing_parameters(SCHEME, kconfig=kconfig).version == "1.2.3+4"


def test_a_header_offset_that_disagrees_with_the_layout_is_a_refusal() -> None:
    """An image linked for another offset boots nowhere; say so, loudly."""
    with pytest.raises(BuildError) as caught:
        signing_parameters(SCHEME, kconfig={"CONFIG_ROM_START_OFFSET": "0x400"})
    assert "1024-byte MCUboot header offset" in caught.value.message
    assert "512" in caught.value.message


def test_the_parameters_survive_a_round_trip() -> None:
    parameters = SigningParameters(header_size=512, align=4, slot_size=933888, version="0.0.0+0")
    assert SigningParameters.from_dict(parameters.to_dict()) == parameters


def test_the_serialized_parameters_use_imgtools_own_option_names() -> None:
    data = signing_parameters(SCHEME, kconfig={}).to_dict()
    assert set(data) == {"version", "header-size", "align", "slot-size"}


def test_incomplete_parameters_are_a_refusal() -> None:
    with pytest.raises(BuildError):
        SigningParameters.from_dict({"version": "0.0.0+0"})


# --------------------------------------------------------------------------
# The whole document
# --------------------------------------------------------------------------


def test_the_manifest_names_the_device_and_the_builder(tmp_path) -> None:
    data = _manifest(tmp_path).to_dict()
    assert data["manifest_version"] == manifest_module.MANIFEST_VERSION
    assert data["builder"]["version"] == __version__
    assert data["builder"]["model_version"] == 1
    assert data["device"] == {
        "name": "bmp180-node",
        "friendly_name": "BMP180 Two-Endpoint Node",
        "board": "nrf7002dk/nrf5340/cpuapp",
        "version": "0.1.0",
    }


def test_the_ota_block_states_the_parameters_before_the_file_exists(tmp_path) -> None:
    """What lets `mcuhome sign` write the .ota on a machine with no config.

    A detached build produces no .ota — there is no signed image to wrap
    yet — but the manifest still has to say what one would look like, or
    the machine that does the signing would have to be told the version and
    the identifiers some other way (ADR 0015 decision 8).
    """
    block = _manifest(tmp_path, ota_image=None).to_dict()["ota"]
    assert block["version"] == "0.1.0"
    assert block["software_version"] == 65536
    assert block["vendor_id"] == pairing.VENDOR_ID
    assert block["product_id"] == pairing.PRODUCT_ID
    assert block["path"] is None


def test_a_board_without_a_staging_slot_gets_no_ota_block() -> None:
    """ADR 0015 decision 5: Matter OTA is board class A only."""
    model = resolve_file(EXAMPLE)
    board = registry.BOARDS[model.device.board]
    scheme = replace(board.update_scheme, matter_ota_kconfig=())
    patched = {**registry.BOARDS, model.device.board: replace(board, update_scheme=scheme)}
    with mock.patch.dict(registry.BOARDS, patched, clear=True):
        assert manifest_module.ota_parameters(model) is None


def test_the_manifest_records_how_the_build_ran(tmp_path) -> None:
    build = _manifest(tmp_path).to_dict()["build"]
    assert build == {
        "snippets": ["matter", "boot-mode"],
        "bootloader_snippets": ["boot-mode"],
        "jobs": 2,
    }


def test_every_image_is_hashed(tmp_path) -> None:
    data = _manifest(tmp_path).to_dict()
    assert [image["name"] for image in data["images"]] == [BOOTLOADER_IMAGE, APP_DIR]
    assert [image["role"] for image in data["images"]] == ["bootloader", "application"]
    for image in data["images"]:
        for entry in image["files"]:
            path = tmp_path / entry["path"]
            assert entry["size"] == path.stat().st_size
            assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_paths_are_relative_to_the_build_directory(tmp_path) -> None:
    """A manifest crosses a network; an absolute host path does not travel."""
    data = _manifest(tmp_path).to_dict()
    for image in data["images"]:
        for entry in image["files"]:
            assert not entry["path"].startswith("/")
            assert entry["path"].startswith("build/")
    assert data["merged"]["path"] == "build/merged_nrf7002dk_nrf5340_cpuapp.hex"


def test_the_flash_size_is_the_unsigned_binary(tmp_path) -> None:
    """A footprint is what the linker put in flash, not header plus trailer."""
    data = _manifest(tmp_path).to_dict()
    application = next(image for image in data["images"] if image["name"] == APP_DIR)
    assert application["flash_bytes"] == 4096


def test_the_signing_block_states_the_command(tmp_path) -> None:
    signing = _manifest(tmp_path).to_dict()["signing"]
    assert signing["tool"] == "imgtool"
    assert signing["image"] == APP_DIR
    assert signing["signature_type"] == registry.SIGNATURE_TYPE
    assert signing["arguments"] == {
        "version": "0.0.0+0",
        "header-size": 512,
        "align": 4,
        "slot-size": 933888,
    }
    assert signing["inputs"] == {
        "bin": f"build/{APP_DIR}/zephyr/zephyr.bin",
        "hex": f"build/{APP_DIR}/zephyr/zephyr.hex",
    }
    assert signing["outputs"] == {
        "bin": f"build/{APP_DIR}/zephyr/zephyr.signed.bin",
        "hex": f"build/{APP_DIR}/zephyr/zephyr.signed.hex",
    }


def test_an_inline_build_says_it_signed_itself(tmp_path) -> None:
    signing = _manifest(tmp_path).to_dict()["signing"]
    assert signing["signed_by_the_build"] is True
    assert signing["signed"] is True


def test_a_detached_build_says_it_did_not(tmp_path) -> None:
    signing = _manifest(tmp_path, signed=False, signed_by_the_build=False).to_dict()["signing"]
    assert signing["signed_by_the_build"] is False
    assert signing["signed"] is False


def test_the_manifest_is_deterministic_apart_from_what_it_measures(tmp_path) -> None:
    first = _manifest(tmp_path).to_json()
    # A second manifest over the same directory: same bytes, key order
    # included. Nothing here may carry a timestamp or a host name.
    build_dir = tmp_path / workspace.BUILD_SUBDIR
    second = build_manifest(
        resolve_file(EXAMPLE),
        out_dir=tmp_path,
        build_dir=build_dir,
        app_image=APP_DIR,
        images=workspace.build_images(build_dir, app_image=APP_DIR),
        snippets=("matter", "boot-mode"),
        bootloader_snippets=("boot-mode",),
        jobs=2,
        signed_by_the_build=True,
        merged=workspace.merged_image(build_dir),
    ).to_json()
    assert first == second


def test_the_manifest_carries_no_commissioning_credentials(tmp_path) -> None:
    """A manifest describes artifacts. Secrets are not artifacts."""
    text = _manifest(tmp_path).to_json()
    model = resolve_file(EXAMPLE)
    assert model.network.pairing is not None
    assert str(model.network.pairing.passcode) not in text
    assert model.network.pairing.salt not in text


# --------------------------------------------------------------------------
# On disk
# --------------------------------------------------------------------------


def test_it_lands_next_to_the_generated_application(tmp_path) -> None:
    path = write_manifest(_manifest(tmp_path), out_dir=tmp_path)
    assert path == tmp_path / MANIFEST_FILE
    assert json.loads(path.read_text("utf-8"))["device"]["name"] == "bmp180-node"


def test_reading_a_manifest_that_is_not_there_is_a_refusal(tmp_path) -> None:
    with pytest.raises(BuildError) as caught:
        read_manifest(tmp_path / MANIFEST_FILE)
    assert MANIFEST_FILE in caught.value.hint


def test_reading_a_broken_manifest_says_it_is_generator_output(tmp_path) -> None:
    path = tmp_path / MANIFEST_FILE
    path.write_text("{not json", "utf-8")
    with pytest.raises(BuildError) as caught:
        read_manifest(path)
    assert "build again" in caught.value.hint


def test_recording_a_signature_updates_the_hashes(tmp_path) -> None:
    write_manifest(_manifest(tmp_path, signed=False, signed_by_the_build=False), out_dir=tmp_path)
    signed = tmp_path / workspace.BUILD_SUBDIR / APP_DIR / "zephyr" / "zephyr.signed.bin"
    signed.write_bytes(b"signed image")

    data = manifest_module.record_signature(
        read_manifest(tmp_path / MANIFEST_FILE), out_dir=tmp_path, files=[signed]
    )
    manifest_module.dump_manifest(data, out_dir=tmp_path)

    reloaded = read_manifest(tmp_path / MANIFEST_FILE)
    assert reloaded["signing"]["signed"] is True
    assert reloaded["signing"]["signed_by_the_build"] is False
    application = next(image for image in reloaded["images"] if image["name"] == APP_DIR)
    entry = next(
        item for item in application["files"] if item["path"].endswith("zephyr.signed.bin")
    )
    assert entry["sha256"] == hashlib.sha256(b"signed image").hexdigest()
