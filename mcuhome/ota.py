# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The device version, and the Matter OTA file the builder wraps around an image.

Two things that are really one: a device's SemVer string (ADR 0005) has to
become a monotonically comparable 32-bit number before a Matter controller
can decide that one image is newer than another, and the same string then
has to reach three different consumers without any of them disagreeing.
ADR 0015 decision 9 fixes the mapping, and :func:`kconfig_lines` is the
single place that applies it — the same "one indivisible group" shape as
:func:`mcuhome.pairing.kconfig_lines`, for the same reason: a build in
which MCUboot's image version and Matter's SoftwareVersion disagree
produces a device that updates to an image the controller then reports as
the wrong version, and nothing warns.

**The .ota file is written here rather than shelled out to CHIP**, and
that is a deliberate departure from the obvious answer. CHIP's
``src/app/ota_image_tool.py`` does exactly this job and lives in the west
workspace — but the workspace is where the *build* happens, and ADR 0015
decision 8 puts signing somewhere else entirely: the private key lives
where the user's controlling instance runs, and a detached build's signed
image therefore only comes into existence during ``mcuhome sign``, on a
machine that per ADR 0003 has no compiler, no west workspace and no Matter
SDK. An .ota wraps the *signed* image, so a builder that could only
produce it during the build could not produce it for the delivery path the
product owner actually asked for.

The format is small enough that this costs eighty lines: a 16-byte fixed
header and a Matter-TLV structure of six fields. ``tests_py/test_ota.py``
pins the result against CHIP's own tool byte for byte whenever the
workspace has one, so "small enough to reimplement" stays a checked claim
rather than an opinion.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from mcuhome.errors import BuildError

__all__ = [
    "DEFAULT_VERSION",
    "DIGEST_TYPE_SHA256",
    "OTA_MAGIC",
    "VERSION_FIELD_MAX",
    "VERSION_PATTERN",
    "OtaImage",
    "describe_version_problem",
    "imgtool_version",
    "kconfig_lines",
    "ota_file_name",
    "parse_version",
    "software_version",
    "write_ota_image",
]

#: What a device configuration gets when it does not say (yaml-schema.md
#: ``device.version``). Pre-1.0 by construction: a first build of a device
#: nobody has versioned yet is a 0.1.0, not a 1.0.0.
DEFAULT_VERSION = "0.1.0"

#: SemVer without pre-release or build metadata. Matter's SoftwareVersion
#: is a plain number with nothing to encode a pre-release tag into, and a
#: version that compares differently on the device than in the
#: configuration is worse than one that cannot be written at all.
VERSION_PATTERN = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
_VERSION_RE = re.compile(VERSION_PATTERN)

#: Largest value each SemVer field can take. ADR 0015 decision 9 packs the
#: three of them into one byte each, so this is the mapping's own limit and
#: not an arbitrary one.
VERSION_FIELD_MAX = 255

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


# --------------------------------------------------------------------------
# The version
# --------------------------------------------------------------------------


def describe_version_problem(text: str) -> str | None:
    """Why *text* is not a usable device version, or None when it is one."""
    match = _VERSION_RE.match(text)
    if match is None:
        return (
            "not a plain SemVer version — write three numbers separated by dots, "
            "for example 1.4.0. Pre-release and build-metadata suffixes have no "
            "place in Matter's SoftwareVersion, which is a single number"
        )
    if any(int(part) > VERSION_FIELD_MAX for part in match.groups()):
        return (
            f"out of range — Matter's SoftwareVersion packs major, minor and patch "
            f"into one byte each (ADR 0015 decision 9), so none of them may exceed "
            f"{VERSION_FIELD_MAX}"
        )
    return None


def parse_version(text: str) -> tuple[int, int, int]:
    """``"1.4.0"`` -> ``(1, 4, 0)``, or a refusal in plain language."""
    problem = describe_version_problem(text)
    if problem is not None:
        raise BuildError(
            f'"{text}" is not a usable device version: {problem}.',
            hint=f"set device.version in the configuration, or leave it out for {DEFAULT_VERSION}",
        )
    major, minor, patch = (int(part) for part in text.split("."))
    return major, minor, patch


def software_version(text: str) -> int:
    """The Matter ``SoftwareVersion`` for a SemVer string (ADR 0015 §9).

    ``major << 24 | minor << 16 | patch << 8``, matching CHIP's own
    ``ota-image.cmake`` convention. The low byte is reserved for a tweak
    counter and is always zero today — which is the point of reserving it:
    a rebuild that has to sort above its predecessor without claiming a new
    patch release has somewhere to go, and does not need this mapping
    re-opened to get there.
    """
    major, minor, patch = parse_version(text)
    return (major << 24) | (minor << 16) | (patch << 8)


def imgtool_version(text: str) -> str:
    """The ``--version`` MCUboot's imgtool stamps into the image header.

    The SemVer string verbatim. imgtool's own format is
    ``major.minor.revision+build``; the build number is left off rather
    than set to zero, because imgtool defaults it to zero anyway and a
    version string that reads the same in the configuration, in the
    manifest and in ``imgtool dumpinfo`` is worth more than symmetry with
    Zephyr's ``0.0.0+0`` default.
    """
    parse_version(text)
    return text


def kconfig_lines(version: str, *, matter: bool) -> list[str]:
    """Every Kconfig symbol the device version consists of.

    Three symbols, three consumers, one source — and they have to agree:

    * ``CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION`` is what MCUboot compares when
      it decides whether a staged image may replace the running one;
    * ``CONFIG_CHIP_DEVICE_SOFTWARE_VERSION`` is what the node reports in
      Basic Information and what an OTA provider compares against the
      image it is offering — an image whose number is not strictly higher
      is never delivered;
    * ``CONFIG_CHIP_DEVICE_SOFTWARE_VERSION_STRING`` is what a user sees.

    *matter* leaves the two CHIP symbols out for a device without a Matter
    stack to put them in.
    """
    lines = [f'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="{imgtool_version(version)}"']
    if matter:
        lines += [
            f"CONFIG_CHIP_DEVICE_SOFTWARE_VERSION={software_version(version)}",
            f'CONFIG_CHIP_DEVICE_SOFTWARE_VERSION_STRING="{version}"',
        ]
    return lines


# --------------------------------------------------------------------------
# The .ota file
# --------------------------------------------------------------------------


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


@dataclass(frozen=True)
class OtaImage:
    """What :func:`write_ota_image` produced, for the build manifest."""

    path: Path
    #: The image inside, i.e. what was wrapped.
    payload: Path
    payload_size: int
    vendor_id: int
    product_id: int
    #: The SemVer string, verbatim.
    version: str
    #: The Matter SoftwareVersion derived from it.
    software_version: int


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
