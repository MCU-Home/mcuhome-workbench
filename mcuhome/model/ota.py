# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The device version: one SemVer string, and everything derived from it.

A device's SemVer string (ADR 0005) has to become a monotonically
comparable 32-bit number before a Matter controller can decide that one
image is newer than another, and the same string then has to reach three
different consumers without any of them disagreeing. ADR 0015 decision 9
fixes the mapping, and :func:`kconfig_lines` is the single place that
applies it — the same "one indivisible group" shape as
:func:`mcuhome.model.pairing.kconfig_lines`, for the same reason: a build in
which MCUboot's image version and Matter's SoftwareVersion disagree
produces a device that updates to an image the controller then reports as
the wrong version, and nothing warns.

**Nothing here writes a file.** The Matter OTA file this version ends up
in the header of is :mod:`mcuhome.workbench.otafile`. The split is ADR 0020's, and
the version is on the model side of it because everything that names a
version needs it and none of them may re-derive it: the resolver
(``resolve.py`` emits the Kconfig group), the validator, the scaffold,
the JSON Schema and the build manifest.

:class:`OtaImage` stays here for the same reason — it is what the writer
produced and what the manifest records, i.e. shared vocabulary, and the
manifest format may not depend on a writer it does not run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mcuhome.model.errors import BuildError

__all__ = [
    "DEFAULT_VERSION",
    "VERSION_FIELD_MAX",
    "VERSION_PATTERN",
    "OtaImage",
    "describe_version_problem",
    "imgtool_version",
    "kconfig_lines",
    "parse_version",
    "software_version",
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


@dataclass(frozen=True)
class OtaImage:
    """What :func:`mcuhome.workbench.otafile.write_ota_image` produced.

    Vocabulary rather than output: the writer fills it in and
    :mod:`mcuhome.model.manifest` records it, which is why it lives with the
    version and not with the writer — a manifest is written on machines
    that never wrap an image.
    """

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
