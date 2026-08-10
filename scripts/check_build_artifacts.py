#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Assert that a finished build left the flashable files behind, well-formed.

The gate behind the Matter build job in ``.github/workflows/ci.yml``: a
build that exits 0 but leaves no flashable image behind is a worse result
than a build that fails, because nothing downstream notices. This script
turns "the compiler was happy" into "the things a device needs exist and
are non-empty, and the build's own description of itself is well-formed".

Usage::

    check_build_artifacts.py <build-dir>

where ``<build-dir>`` is what ``mcuhome build --build-dir`` was given.

**Two build shapes, chosen by which description file is present** — the
same fork ``mcuhome sign`` makes (``mcuhome/workbench/imgtool.py``):

* the **default** ``mcuhome build`` drives the ``local`` backend through
  the build container and delivers ``build-report.json`` (the §7.2.1
  report) beside the artifacts, which the host then signs. Its artifact
  set is flat: ``firmware.{hex,bin}`` (unsigned), ``bootloader.hex`` when
  the build produced one, ``firmware.signed.{hex,bin}`` (host-signed) and
  one ``<device>-<version>.ota`` wrapped from the signed image.
* ``--method local-dev`` compiles on the host with ``west`` and writes the fuller
  ``build-manifest.json`` over a sysbuild layout, signing inline.

``build-report.json`` present selects the first; ``build-manifest.json``
present selects the second. They are mutually exclusive — one build
method writes one of them — so presence is a clean selector.

**Why the report shape is checked by presence, not by hash.** Unlike the
manifest, the §7.2.1 report carries *no* per-artifact hash list (that was
cut vs the old ``build-manifest.json``), so there is no recorded value to
re-hash against. There is also no need for one here: the build container
already re-hashed every artifact on egress against the report it emits
(build-container-contract §5.3), so a second hash oracle in CI would only
re-check what the container already guaranteed. CI's job for this shape is
therefore narrower and exactly right — "the flashable files exist and are
non-empty, and the report is a well-formed §7.2.1 document" — which is
what catches a build that silently produced nothing, or a report a signer
would refuse. The manifest shape, which *does* record a size and SHA-256
per file, is still verified against them.

What the **report shape** requires (each present and non-empty):

* ``build-report.json`` — and it must parse, carry ``report`` == 1, a
  ``signing`` block whose ``signature_type`` is ``ecdsa-p256`` and whose
  ``arguments`` is an object holding all four imgtool keys (``version``,
  ``header-size``, ``align``, ``slot-size``). Without it a detached signer
  has nothing to read, and §7.2 requires exactly one report.
* ``firmware.hex`` and ``firmware.bin`` — the unsigned firmware the
  container declared with role ``firmware`` (§7.2).
* ``firmware.signed.hex`` and ``firmware.signed.bin`` — what the host
  signer produced; an unsigned application is one MCUboot refuses to
  chain-load.
* exactly one ``*.ota`` — the Matter update image wrapped from the signed
  binary.
* ``bootloader.hex`` is checked non-empty *when present*, but a missing
  bootloader is not a failure by itself: §7.2 requires at least one
  firmware artifact and exactly one report, and makes the bootloader
  optional (a board without an MCUboot member, a build that does not
  deliver one).

What the **manifest shape** requires: the bootloader image, the signed
application in both formats, the merged hex and ``build-manifest.json``
itself, and every file the manifest lists verified by size and SHA-256 —
which is what catches a truncated or half-written artifact there.

The image and file names below are written out rather than imported from
the code under test: this is an independent oracle for a regression gate,
and one that imported its expectations from the code that produced the
artifacts would follow that code silently wherever it went. Standard
library only, for the same reason.

Exit status: 0 when the artifact set is complete, 1 with findings (one
per line), 2 on usage errors.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# --- the default (container) shape --------------------------------------

#: Delivered by the ``local`` backend beside the unsigned firmware, and the
#: selector for this shape. The §7.2.1 report a host signer consumes.
BUILD_REPORT_FILE = "build-report.json"

#: The one report format version this gate understands (§7.2.1).
REPORT_VERSION = 1

#: The one signature algorithm MCUHome images carry
#: (``mcuhome.model.registry.SIGNATURE_TYPE``).
SIGNATURE_TYPE = "ecdsa-p256"

#: imgtool's own option names, the keys a §7.2.1 ``signing.arguments``
#: object must carry so a detached signer can turn it back into a command
#: (``mcuhome.model.manifest.SigningParameters.to_dict``).
SIGNING_ARGUMENT_KEYS = ("version", "header-size", "align", "slot-size")

#: The flashable files the container path must leave behind: the unsigned
#: firmware in both encodings, and the host-signed forms of each.
REPORT_REQUIRED_FILES = (
    "firmware.hex",
    "firmware.bin",
    "firmware.signed.hex",
    "firmware.signed.bin",
)

#: The bootloader image, when the build delivered one. Optional: §7.2
#: requires firmware + report, not a bootloader.
BOOTLOADER_FILE = "bootloader.hex"

#: The Matter update image, wrapped from the signed binary and named
#: ``<device>-<version>.ota`` — matched by suffix, of which there is one.
OTA_GLOB = "*.ota"

# --- the local-dev (west/sysbuild) shape --------------------------------

#: Written by ``mcuhome.compiler.report.write_manifest`` into a local-dev
#: build directory, and the selector for that shape.
MANIFEST_FILE = "build-manifest.json"

#: Sysbuild's name for the bootloader image, fixed by Zephyr.
BOOTLOADER_IMAGE = "mcuboot"

#: The role ``mcuhome.compiler.workspace.build_images`` gives the application image.
APPLICATION_ROLE = "application"

#: File names that must be present per image role. The bootloader is not
#: signed — it *is* the trust anchor — so only the application has signed
#: forms. Deliberately the flashable forms only: which additional formats
#: Zephyr emits is a Kconfig question, and a gate that asserted all of
#: them would fail for a reason that is not a defect.
REQUIRED_FILES = {
    "bootloader": ("zephyr.hex",),
    APPLICATION_ROLE: ("zephyr.signed.hex", "zephyr.signed.bin"),
}

#: Read in blocks: a firmware image is small, the .ota is not necessarily,
#: and there is no reason to hold either in memory twice.
_HASH_BLOCK = 1 << 20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_HASH_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _nonempty(path: Path, *, what: str) -> list[str]:
    """A file that must exist and hold bytes, or the finding that says so."""
    if not path.is_file():
        return [f"{what} is missing"]
    if path.stat().st_size == 0:
        return [f"{what} is empty"]
    return []


# --- the default (container) shape --------------------------------------


def _check_report_document(report_path: Path) -> list[str]:
    """The §7.2.1 ``build-report.json`` itself: parses, version, signing block."""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"{BUILD_REPORT_FILE} is not readable JSON: {error}"]
    if not isinstance(report, dict):
        return [f"{BUILD_REPORT_FILE} does not describe a build"]

    findings: list[str] = []
    version = report.get("report")
    if version != REPORT_VERSION:
        findings.append(
            f"{BUILD_REPORT_FILE} is report format version {version!r}, not {REPORT_VERSION}"
        )

    signing = report.get("signing")
    if not isinstance(signing, dict):
        findings.append(f"{BUILD_REPORT_FILE} carries no signing block")
        return findings

    signature_type = signing.get("signature_type")
    if signature_type != SIGNATURE_TYPE:
        findings.append(
            f"{BUILD_REPORT_FILE} signs with signature_type {signature_type!r}, "
            f"not {SIGNATURE_TYPE!r}"
        )

    arguments = signing.get("arguments")
    if not isinstance(arguments, dict):
        findings.append(f"{BUILD_REPORT_FILE} signing.arguments is not an object")
    else:
        missing = [key for key in SIGNING_ARGUMENT_KEYS if key not in arguments]
        if missing:
            findings.append(
                f"{BUILD_REPORT_FILE} signing.arguments is missing {', '.join(missing)}"
            )
    return findings


def check_report(out_dir: Path) -> list[str]:
    """Every finding about a container-path build directory."""
    findings = _check_report_document(out_dir / BUILD_REPORT_FILE)

    for name in REPORT_REQUIRED_FILES:
        findings += _nonempty(out_dir / name, what=name)

    bootloader = out_dir / BOOTLOADER_FILE
    if bootloader.is_file() and bootloader.stat().st_size == 0:
        findings.append(f"{BOOTLOADER_FILE} is present but empty")

    otas = sorted(out_dir.glob(OTA_GLOB))
    if not otas:
        findings.append("no .ota image: the build wrapped none from the signed firmware")
    elif len(otas) > 1:
        names = ", ".join(path.name for path in otas)
        findings.append(f"expected exactly one .ota image, found {len(otas)}: {names}")
    else:
        findings += _nonempty(otas[0], what=otas[0].name)
    return findings


def describe_report(out_dir: Path) -> str:
    """One line naming what was checked in a container-path build directory."""
    otas = sorted(out_dir.glob(OTA_GLOB))
    ota = otas[0].name if otas else "no .ota"
    present = [
        name for name in (BOOTLOADER_FILE, *REPORT_REQUIRED_FILES) if (out_dir / name).is_file()
    ]
    return f"{ota}: {', '.join(present)}"


# --- the local-dev (west/sysbuild) shape --------------------------------


def check_file(entry: dict[str, Any], *, out_dir: Path, what: str) -> list[str]:
    """Verify one manifest file entry against the file on disk."""
    recorded = entry.get("path")
    if not isinstance(recorded, str) or not recorded:
        return [f"{what}: the manifest records no path"]
    path = out_dir / recorded
    if not path.is_file():
        return [f"{what}: {recorded} is missing"]

    size = path.stat().st_size
    if size == 0:
        return [f"{what}: {recorded} is empty"]

    findings: list[str] = []
    recorded_size = entry.get("size")
    if recorded_size != size:
        findings.append(f"{what}: {recorded} is {size} bytes, not {recorded_size}")
    digest = _sha256(path)
    if entry.get("sha256") != digest:
        findings.append(f"{what}: {recorded} hashes to {digest}, not to {entry.get('sha256')}")
    return findings


def _file_names(image: dict[str, Any]) -> set[str]:
    """The bare file names one image entry lists."""
    return {Path(entry.get("path", "")).name for entry in image.get("files", [])}


def check_images(manifest: dict[str, Any], *, out_dir: Path) -> list[str]:
    """The bootloader and the signed application, with all their files."""
    findings: list[str] = []
    images = {image.get("name"): image for image in manifest.get("images", [])}

    bootloader = images.get(BOOTLOADER_IMAGE)
    if bootloader is None:
        findings.append(f"no {BOOTLOADER_IMAGE} image: the build produced no bootloader")

    applications = [entry for entry in images.values() if entry.get("role") == APPLICATION_ROLE]
    if len(applications) != 1:
        findings.append(
            f"expected exactly one image with role {APPLICATION_ROLE!r}, "
            f"the manifest has {len(applications)}"
        )

    for image in [entry for entry in (bootloader, *applications) if entry is not None]:
        name = image.get("name")
        role = image.get("role")
        present = _file_names(image)
        for required in REQUIRED_FILES.get(str(role), ()):
            if required not in present:
                findings.append(f"image {name} ({role}): {required} was not produced")
        for entry in image.get("files", []):
            findings += check_file(entry, out_dir=out_dir, what=f"image {name}")
    return findings


def check_manifest(out_dir: Path) -> list[str]:
    """Every finding about a local-dev build directory, in reading order."""
    manifest_path = out_dir / MANIFEST_FILE
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"{manifest_path} is not readable JSON: {error}"]

    findings = check_images(manifest, out_dir=out_dir)

    merged = manifest.get("merged")
    if merged is None:
        findings.append(
            "no merged hex: sysbuild wrote none, or wrote more than one and the "
            "builder refused to pick between them"
        )
    else:
        findings += check_file(merged, out_dir=out_dir, what="merged image")

    signing = manifest.get("signing")
    if signing is None:
        findings.append("no signing block: the manifest cannot say how the image was signed")
    elif not signing.get("signed"):
        findings.append("the application image is unsigned (signing.signed is false)")

    ota = manifest.get("ota")
    if ota is not None and ota.get("path") is not None:
        findings += check_file(ota, out_dir=out_dir, what="OTA image")
    return findings


def describe_manifest(out_dir: Path) -> str:
    """One line naming what was checked in a local-dev build directory."""
    manifest = json.loads((out_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    device = manifest.get("device", {})
    images = ", ".join(str(image.get("name")) for image in manifest.get("images", []))
    return f"{device.get('name')} for {device.get('board')}: {images}"


# --- dispatch -----------------------------------------------------------


def check(out_dir: Path) -> list[str]:
    """Every finding about *out_dir*, whichever build shape produced it."""
    if (out_dir / BUILD_REPORT_FILE).is_file():
        return check_report(out_dir)
    if (out_dir / MANIFEST_FILE).is_file():
        return check_manifest(out_dir)
    return [
        f"neither {BUILD_REPORT_FILE} nor {MANIFEST_FILE} is in {out_dir}: "
        "this is not a finished build directory"
    ]


def describe(out_dir: Path) -> str:
    """One line naming what was checked, for the log."""
    if (out_dir / BUILD_REPORT_FILE).is_file():
        return describe_report(out_dir)
    return describe_manifest(out_dir)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <build-dir>", file=sys.stderr)
        return 2
    out_dir = Path(argv[1]).resolve()

    findings = check(out_dir)
    if not findings:
        print(f"Artifact set complete in {out_dir}")
        print(f"  {describe(out_dir)}")
        return 0

    print(f"Incomplete build output in {out_dir}:\n")
    for finding in findings:
        print(f"  {finding}")
    print(
        "\nA build that exits 0 without a complete artifact set is a failure "
        "nothing downstream would notice: mcuhome sign, the OTA wrapper and "
        "every flashing path expect the files a finished build names to be there."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
