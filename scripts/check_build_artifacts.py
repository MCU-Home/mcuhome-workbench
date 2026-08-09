#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Assert that a finished build produced what its manifest says it did.

The gate behind the Matter build job in ``.github/workflows/ci.yml``: a
build that exits 0 but leaves no flashable image behind is a worse result
than a build that fails, because nothing downstream notices. This script
turns "the compiler was happy" into "the things a device needs exist, are
non-empty, and hash to what the manifest recorded".

Usage::

    check_build_artifacts.py <build-dir>

where ``<build-dir>`` is what ``mcuhome build --build-dir`` was given —
the directory holding ``build-manifest.json``. Every path inside the
manifest is relative to it.

What is required, and why each one:

* ``build-manifest.json`` itself — the description of the artifact set
  (builder-pipeline.md §7); without it nothing else can be checked, and
  ``mcuhome sign`` and every flashing path have nothing to read.
* the MCUboot image — the trust anchor, and what a factory flash starts.
* the signed application image, in both formats — an unsigned
  application is an image MCUboot refuses to chain-load.
* the merged hex — sysbuild's every-image-in-one-file form, which is
  what a first-time flash writes.

Every file the manifest lists (including the ``.ota``, when the build
wrote one) is additionally verified by size and SHA-256, which is what
catches a truncated or half-written artifact rather than an absent one.

The image and file names below are written out rather than imported from
:mod:`mcuhome.compiler.generate`: this is an independent oracle for a regression
gate, and one that imported its expectations from the code under test
would follow that code silently wherever it went. Standard library only,
for the same reason.

Exit status: 0 when the artifact set is complete, 1 with findings (one
per line), 2 on usage errors.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

#: Written by ``mcuhome.compiler.report.write_manifest`` into the build directory.
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


def check(out_dir: Path) -> list[str]:
    """Every finding about *out_dir*, in the order a reader wants them."""
    manifest_path = out_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        return [f"{manifest_path} is missing: this is not a finished build directory"]
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


def describe(out_dir: Path) -> str:
    """One line naming what was checked, for the log."""
    manifest = json.loads((out_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    device = manifest.get("device", {})
    images = ", ".join(str(image.get("name")) for image in manifest.get("images", []))
    return f"{device.get('name')} for {device.get('board')}: {images}"


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
        "every flashing path read the manifest and expect the files it names "
        "to be there."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
