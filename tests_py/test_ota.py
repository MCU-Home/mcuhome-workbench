# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The device version (:mod:`mcuhome.ota`) and the Matter OTA file
(:mod:`mcuhome.otafile`).

Three things are worth pinning here and each one is a different kind of
claim:

* the **SemVer to SoftwareVersion mapping** is fixed by ADR 0015
  decision 9 and published to the world in every image's Basic
  Information cluster — changing it silently would make every device in
  the field disagree with every image built after the change;
* the **.ota file is byte-identical to CHIP's own tool's output**, which
  is the check that keeps "the format is small enough to reimplement"
  honest (the module docstring explains why MCUHome writes it itself);
* a **version out of range is refused with an explanation**, because
  ``256.0.0`` silently becoming ``0.0.0`` on the wire is exactly the
  class of bug the whole one-source-per-group discipline exists to
  prevent.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, resolve_file

from mcuhome import ota, otafile, pairing
from mcuhome.errors import BuildError

EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"

#: CHIP's own OTA image tool, when this checkout sits in a west workspace
#: that has the Matter SDK. Absent on a machine that only installed the
#: builder, which is the normal case for the interop test below.
CHIP_OTA_TOOL = (
    Path(__file__).resolve().parents[2]
    / "modules"
    / "lib"
    / "connectedhomeip"
    / "src"
    / "app"
    / "ota_image_tool.py"
)


# --------------------------------------------------------------------------
# The version
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.0.0", 0x00000000),
        ("0.1.0", 0x00010000),
        ("1.0.0", 0x01000000),
        ("1.2.3", 0x01020300),
        ("255.255.255", 0xFFFFFF00),
    ],
)
def test_the_semver_mapping_is_the_one_the_adr_fixed(version: str, expected: int) -> None:
    """``major << 24 | minor << 16 | patch << 8`` (ADR 0015 decision 9)."""
    assert ota.software_version(version) == expected


def test_the_low_byte_is_reserved_and_therefore_always_zero() -> None:
    """A tweak counter has somewhere to go without re-opening the mapping."""
    for version in ("0.0.1", "9.9.9", "255.255.255"):
        assert ota.software_version(version) & 0xFF == 0


def test_versions_compare_the_way_semver_does() -> None:
    """The only property Matter actually needs from the mapping."""
    ordered = ["0.0.1", "0.1.0", "0.1.1", "0.2.0", "1.0.0", "1.0.1", "2.0.0"]
    numbers = [ota.software_version(item) for item in ordered]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)


@pytest.mark.parametrize(
    "version",
    ["1.0", "1.0.0.0", "v1.0.0", "1.0.0-rc1", "1.0.0+7", "01.0.0", "", "one.two.three"],
)
def test_a_version_that_is_not_plain_semver_is_refused(version: str) -> None:
    assert ota.describe_version_problem(version) is not None


@pytest.mark.parametrize("version", ["256.0.0", "0.256.0", "0.0.256", "1000.0.0"])
def test_a_field_that_does_not_fit_in_a_byte_is_refused(version: str) -> None:
    """The mapping's own limit, and the message has to say so."""
    problem = ota.describe_version_problem(version)
    assert problem is not None
    assert "one byte each" in problem


def test_parse_version_refuses_with_a_hint() -> None:
    with pytest.raises(BuildError) as error:
        ota.parse_version("1.0")
    assert "device.version" in str(error.value.hint)


def test_the_kconfig_group_states_all_three_symbols_from_one_string() -> None:
    """MCUboot's version and Matter's cannot be allowed to disagree."""
    lines = ota.kconfig_lines("1.2.3", matter=True)
    assert lines == [
        'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="1.2.3"',
        "CONFIG_CHIP_DEVICE_SOFTWARE_VERSION=16909056",
        'CONFIG_CHIP_DEVICE_SOFTWARE_VERSION_STRING="1.2.3"',
    ]


def test_a_device_without_matter_gets_only_the_mcuboot_symbol() -> None:
    assert ota.kconfig_lines("1.2.3", matter=False) == [
        'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="1.2.3"'
    ]


def test_the_resolved_model_carries_the_default_version() -> None:
    model = resolve_file(EXAMPLE)
    assert model.device.version == ota.DEFAULT_VERSION
    assert f'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="{ota.DEFAULT_VERSION}"' in model.build.kconfig


# --------------------------------------------------------------------------
# The .ota file
# --------------------------------------------------------------------------


def _payload(tmp_path: Path, size: int = 4096) -> Path:
    path = tmp_path / "zephyr.signed.bin"
    path.write_bytes(bytes(range(256)) * (size // 256))
    return path


def test_the_file_name_carries_the_version(tmp_path: Path) -> None:
    """An OTA provider directory holds every image it might ever serve."""
    assert otafile.ota_file_name("bedroom-climate", "1.4.0") == "bedroom-climate-1.4.0.ota"


def test_the_image_is_the_header_plus_the_payload_verbatim(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    result = otafile.write_ota_image(
        payload=payload,
        output=tmp_path / "out.ota",
        vendor_id=0xFFF1,
        product_id=0x8000,
        version="1.2.3",
    )
    raw = result.path.read_bytes()
    magic, total, header_size = struct.unpack("<IQI", raw[:16])

    assert magic == otafile.OTA_MAGIC
    assert total == len(raw)
    assert header_size == len(raw) - 16 - payload.stat().st_size
    # Byte for byte: the payload is what MCUboot verifies, so nothing may
    # touch it on the way in.
    assert raw[16 + header_size :] == payload.read_bytes()
    assert result.software_version == 0x01020300
    assert result.payload_size == payload.stat().st_size


def _decode_header_tlv(tlv: bytes) -> list[tuple[int, object]]:
    """Walk the header structure, returning (context tag, value) in order.

    Small enough to be obvious and independent of the module under test,
    which is the point: a decoder that shared code with the writer would
    agree with it about a mistake.
    """
    assert tlv[0] == 0x15, "an anonymous structure"
    assert tlv[-1] == 0x18, "closed"
    out: list[tuple[int, object]] = []
    offset = 1
    while tlv[offset] != 0x18:
        control = tlv[offset]
        assert control & 0xE0 == 0x20, "every member carries a context tag"
        element = control & 0x1F
        tag = tlv[offset + 1]
        offset += 2
        width = 1 << (element & 0x03)
        if 0x04 <= element <= 0x07:  # unsigned integer
            out.append((tag, int.from_bytes(tlv[offset : offset + width], "little")))
            offset += width
        elif 0x0C <= element <= 0x13:  # UTF-8 or byte string
            length = int.from_bytes(tlv[offset : offset + width], "little")
            offset += width
            body = tlv[offset : offset + length]
            out.append((tag, body.decode("utf-8") if element <= 0x0F else body))
            offset += length
        else:  # pragma: no cover - the writer emits nothing else
            raise AssertionError(f"unexpected element type {element:#04x}")
    return out


def test_the_header_is_the_shape_the_firmware_parser_expects(tmp_path: Path) -> None:
    """The two halves of the format agree.

    The device-side parser lives in C
    (components/matter/src/ota_image_header.c, exercised by
    tests/ota_image_header/) and its golden inputs came from CHIP's tool.
    This is the other direction: what the builder writes carries the five
    fields that parser reads, with the mandatory ones present and the tags
    in the ascending order CHIP's own TLV writer produces.
    """
    payload = _payload(tmp_path)
    result = otafile.write_ota_image(
        payload=payload,
        output=tmp_path / "out.ota",
        vendor_id=0xFFF1,
        product_id=0x8000,
        version="0.1.0",
    )
    raw = result.path.read_bytes()
    _, _, header_size = struct.unpack("<IQI", raw[:16])
    fields = _decode_header_tlv(raw[16 : 16 + header_size])

    tags = [tag for tag, _ in fields]
    assert tags == sorted(tags), "CHIP's TLVWriter sorts a structure by tag"
    values = dict(fields)
    assert values[0] == 0xFFF1  # vendor
    assert values[1] == 0x8000  # product
    assert values[2] == 0x00010000  # software version
    assert values[3] == "0.1.0"  # version string
    assert values[4] == payload.stat().st_size  # payload size
    assert values[8] == otafile.DIGEST_TYPE_SHA256
    assert len(values[9]) == 32  # digest


def test_an_empty_image_is_refused(tmp_path: Path) -> None:
    payload = tmp_path / "zephyr.signed.bin"
    payload.write_bytes(b"")
    with pytest.raises(BuildError, match="nothing to update to"):
        otafile.write_ota_image(
            payload=payload,
            output=tmp_path / "out.ota",
            vendor_id=1,
            product_id=1,
            version="0.1.0",
        )


def test_a_missing_image_says_to_sign_first(tmp_path: Path) -> None:
    with pytest.raises(BuildError) as error:
        otafile.write_ota_image(
            payload=tmp_path / "nothing.bin",
            output=tmp_path / "out.ota",
            vendor_id=1,
            product_id=1,
            version="0.1.0",
        )
    assert "mcuhome sign" in str(error.value.hint)


def test_writing_the_same_image_twice_gives_the_same_bytes(tmp_path: Path) -> None:
    """Determinism, like every other artifact the builder produces."""
    payload = _payload(tmp_path)
    arguments = {
        "payload": payload,
        "vendor_id": pairing.VENDOR_ID,
        "product_id": pairing.PRODUCT_ID,
        "version": "0.1.0",
    }
    first = otafile.write_ota_image(output=tmp_path / "a.ota", **arguments)
    second = otafile.write_ota_image(output=tmp_path / "b.ota", **arguments)
    assert first.path.read_bytes() == second.path.read_bytes()


@pytest.mark.skipif(not CHIP_OTA_TOOL.is_file(), reason="no Matter SDK in this workspace")
@pytest.mark.parametrize("version", ["0.1.0", "1.2.3", "255.255.255"])
def test_the_file_is_byte_identical_to_the_one_chips_tool_writes(
    tmp_path: Path, version: str
) -> None:
    """The check that keeps the reimplementation honest.

    MCUHome writes the .ota itself so that ``mcuhome sign`` can produce one
    on a machine with no Matter SDK (ADR 0015 decision 8 puts signing where
    the key is). That is only defensible if the result is the same file
    CHIP's tool would have written, so wherever the SDK *is* available —
    a contributor's workspace, CI — this compares the bytes.
    """
    payload = _payload(tmp_path)
    mine = otafile.write_ota_image(
        payload=payload,
        output=tmp_path / "mine.ota",
        vendor_id=pairing.VENDOR_ID,
        product_id=pairing.PRODUCT_ID,
        version=version,
    )
    theirs = tmp_path / "theirs.ota"
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            str(CHIP_OTA_TOOL),
            "create",
            "-v",
            str(pairing.VENDOR_ID),
            "-p",
            str(pairing.PRODUCT_ID),
            "-vn",
            str(ota.software_version(version)),
            "-vs",
            version,
            "-da",
            "sha256",
            str(payload),
            str(theirs),
        ],
        check=True,
        capture_output=True,
    )
    assert mine.path.read_bytes() == theirs.read_bytes()
