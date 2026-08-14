# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The Matter OTA file (:mod:`mcuhome.workbench.otafile`).

The workbench half of the subject; the version the file carries comes
from :mod:`mcuhome.model.ota` and is tested in ``mcuhome-sdk``'s
``test_ota.py``.

What is worth pinning here is one claim: the **.ota file is
byte-identical to CHIP's own tool's output**, which is the check that
keeps "the format is small enough to reimplement" honest (the module
docstring explains why MCUHome writes it itself).
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest
from mcuhome.model import ota, pairing
from mcuhome.model.errors import BuildError

from mcuhome.workbench import otafile

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
