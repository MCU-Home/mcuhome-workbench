# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The Matter OTA file the builder wraps around a signed image.

**Written here rather than shelled out to CHIP**, and that is a
deliberate departure from the obvious answer. CHIP's
``src/app/ota_image_tool.py`` does exactly this job and lives in the west
workspace — but the workspace is where the *build* happens, and ADR 0015
decision 8 puts signing somewhere else entirely: the private key lives
where the user's controlling instance runs, and a detached build's signed
image therefore only comes into existence during ``mcuhome sign``, on a
machine that per ADR 0003 has no compiler, no west workspace and no Matter
SDK. An .ota wraps the *signed* image, so a builder that could only
produce it during the build could not produce it for the delivery path the
product owner actually asked for.

That same sentence is why this module sits beside signing rather than
inside the compiler package (ADR 0020): the machine that wraps an image
is the machine that signed it, and it has no toolchain. What it needs
from the build is the manifest's OTA block, nothing more.

The format is small enough that this costs eighty lines: a 16-byte fixed
header and a Matter-TLV structure of six fields. ``tests_py/test_ota.py``
pins the result against CHIP's own tool byte for byte whenever the
workspace has one, so "small enough to reimplement" stays a checked claim
rather than an opinion.
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

from mcuhome.errors import BuildError
from mcuhome.ota import OtaImage, software_version

__all__ = [
    "DIGEST_TYPE_SHA256",
    "OTA_MAGIC",
    "header_tlv",
    "ota_file_name",
    "write_ota_image",
]

#: File signature of a Matter OTA image (Matter 1.4 §11.21.1).
OTA_MAGIC = 0x1BEEF11E

#: ``OTAImageDigestType::kSha256``.
DIGEST_TYPE_SHA256 = 1

#: ``<IQI``: magic, total size, header TLV size — all little-endian.
_FIXED_HEADER = "<IQI"

#: Matter TLV context tags of the OTA header structure.
_TAG_VENDOR_ID = 0
_TAG_PRODUCT_ID = 1
_TAG_VERSION = 2
_TAG_VERSION_STRING = 3
_TAG_PAYLOAD_SIZE = 4
_TAG_DIGEST_TYPE = 8
_TAG_DIGEST = 9

#: Matter TLV element types, context-tagged (tag control 0x20).
_TYPE_UINT = {1: 0x24, 2: 0x25, 4: 0x26, 8: 0x27}
_TYPE_UTF8 = {1: 0x2C, 2: 0x2D, 4: 0x2E, 8: 0x2F}
_TYPE_BYTES = {1: 0x30, 2: 0x31, 4: 0x32, 8: 0x33}
_ANONYMOUS_STRUCT = 0x15
_END_OF_CONTAINER = 0x18

#: Read in blocks: a Matter image is most of a megabyte and there is no
#: reason for it to be in memory twice (same rule as mcuhome.manifest).
_BLOCK = 1 << 20


def ota_file_name(device: str, version: str) -> str:
    """``bedroom-climate`` at 0.1.0 -> ``bedroom-climate-0.1.0.ota``.

    Version in the name, because an OTA provider directory holds every
    image it might ever serve and a file called ``<device>.ota`` would mean
    the previous one had to be deleted before the next one could be put
    there — exactly at the moment a rollback might be wanted.
    """
    return f"{device}-{version}.ota"


def _tlv_uint(tag: int, value: int) -> bytes:
    """One context-tagged unsigned integer, in the narrowest legal width.

    Matter TLV allows 1, 2, 4 and 8 bytes, and CHIP's own writer always
    picks the smallest that fits. Matching that is what makes the output of
    this module byte-identical to ``ota_image_tool.py``'s.
    """
    for width in (1, 2, 4, 8):
        if value < (1 << (8 * width)):
            return bytes([_TYPE_UINT[width], tag]) + value.to_bytes(width, "little")
    raise BuildError(  # pragma: no cover - no caller can produce one
        f"The value {value} does not fit in a Matter TLV unsigned integer.",
        hint="this is a builder bug worth reporting",
    )


def _tlv_length_prefixed(types: dict[int, int], tag: int, payload: bytes) -> bytes:
    for width in (1, 2, 4, 8):
        if len(payload) < (1 << (8 * width)):
            header = bytes([types[width], tag]) + len(payload).to_bytes(width, "little")
            return header + payload
    raise BuildError(  # pragma: no cover - no caller can produce one
        "A Matter TLV string is longer than the format allows.",
        hint="this is a builder bug worth reporting",
    )


def header_tlv(
    *,
    vendor_id: int,
    product_id: int,
    version: int,
    version_string: str,
    payload_size: int,
    digest: bytes,
) -> bytes:
    """The header structure, in ascending tag order.

    Ascending order is not decoration: CHIP's ``TLVWriter`` sorts a
    structure's members by tag, so writing them in any other order would
    produce a valid file that is not the same file its tool writes — and
    the interop test in ``tests_py/test_ota.py`` compares bytes.
    """
    body = b"".join(
        [
            _tlv_uint(_TAG_VENDOR_ID, vendor_id),
            _tlv_uint(_TAG_PRODUCT_ID, product_id),
            _tlv_uint(_TAG_VERSION, version),
            _tlv_length_prefixed(_TYPE_UTF8, _TAG_VERSION_STRING, version_string.encode("utf-8")),
            _tlv_uint(_TAG_PAYLOAD_SIZE, payload_size),
            _tlv_uint(_TAG_DIGEST_TYPE, DIGEST_TYPE_SHA256),
            _tlv_length_prefixed(_TYPE_BYTES, _TAG_DIGEST, digest),
        ]
    )
    return bytes([_ANONYMOUS_STRUCT]) + body + bytes([_END_OF_CONTAINER])


def write_ota_image(
    *,
    payload: Path,
    output: Path,
    vendor_id: int,
    product_id: int,
    version: str,
) -> OtaImage:
    """Wrap a signed application image in a Matter OTA file.

    *payload* has to be the **signed** binary. An unsigned one produces a
    perfectly valid .ota that the device downloads, stages, reboots into
    and then rejects at the bootloader — the digest in the header proves
    nothing about origin, and CHIP does not check it on any platform
    anyway (ADR 0015 decision 6). MCUboot's signature is the only trust
    anchor in this path, so the wrapper's job is to carry it, not to
    replace it.
    """
    number = software_version(version)
    digest = hashlib.sha256()
    size = 0
    try:
        with payload.open("rb") as handle:
            while block := handle.read(_BLOCK):
                digest.update(block)
                size += len(block)
    except OSError as error:
        raise BuildError(
            f"MCUHome cannot read the image {payload}: {error.strerror}.",
            hint=(
                "the Matter OTA file wraps the signed application image; a build "
                "that produced none has nothing to wrap. Sign it first:\n"
                "    mcuhome sign <build directory>"
            ),
        ) from error

    if size == 0:
        raise BuildError(
            f"The image {payload} is empty, so there is nothing to update to.",
            hint="build again; a build that produced no image is a build that failed",
        )

    tlv = header_tlv(
        vendor_id=vendor_id,
        product_id=product_id,
        version=number,
        version_string=version,
        payload_size=size,
        digest=digest.digest(),
    )
    fixed = struct.pack(
        _FIXED_HEADER, OTA_MAGIC, struct.calcsize(_FIXED_HEADER) + len(tlv) + size, len(tlv)
    )

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("wb") as out, payload.open("rb") as handle:
            out.write(fixed)
            out.write(tlv)
            while block := handle.read(_BLOCK):
                out.write(block)
    except OSError as error:
        raise BuildError(
            f"The Matter OTA file {output} cannot be written: {error.strerror}.",
            hint="pick a writable location with --build-dir",
        ) from error

    return OtaImage(
        path=output,
        payload=payload,
        payload_size=size,
        vendor_id=vendor_id,
        product_id=product_id,
        version=version,
        software_version=number,
    )
