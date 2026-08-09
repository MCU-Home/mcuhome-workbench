# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Describing a finished build: ``build-manifest.json``, measured.

The other half of :mod:`mcuhome.model.manifest`. That module is the format —
what the document looks like, how it round-trips, what a signer may edit
in one. This one *produces* one, and everything it needs to do so is a
property of the machine that compiled: a build directory to walk, the
artifact lists stage 5 collected (:mod:`mcuhome.compiler.workspace`), and the
board layout the builder itself rendered into the overlay.

That is the whole reason for the cut (ADR 0020): a dashboard reads
manifests and must never carry a toolchain, and the build server shares
the vocabulary without the build logic. Both get the format; only the
compiler gets this.

**The signing block is a contract, not a report.** Three of its four
``imgtool sign`` arguments come from the registry — ADR 0015 decision 2
makes the partition table per-board data, and the builder is what wrote
it into the overlay — and the fourth from the application image's own
Kconfig, because imgtool's ``--version`` is the one parameter the builder
does not itself decide.
"""

from __future__ import annotations

from pathlib import Path

from mcuhome.compiler import workspace
from mcuhome.model import __version__, ota, registry
from mcuhome.model.errors import BuildError
from mcuhome.model.manifest import (
    MANIFEST_FILE,
    BuildManifest,
    FileEntry,
    ImageEntry,
    SigningBlock,
    SigningParameters,
    ota_entry,
    ota_parameters,
)
from mcuhome.model.model import DeviceModel

__all__ = [
    "HEADER_SIZE_SYMBOL",
    "KCONFIG_FILE",
    "SIGN_VERSION_DEFAULT",
    "SIGN_VERSION_SYMBOL",
    "build_manifest",
    "kconfig_path",
    "read_kconfig",
    "signing_parameters",
    "write_manifest",
]

#: Kconfig symbol carrying imgtool's ``--version`` on the application
#: image, and the value Zephyr defaults it to. Read from the built image
#: rather than assumed, because it is the one of the four parameters the
#: builder does not itself decide.
SIGN_VERSION_SYMBOL = "CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION"
SIGN_VERSION_DEFAULT = "0.0.0+0"

#: Kconfig symbol carrying the MCUboot header offset the application was
#: linked with — imgtool's ``--header-size``. Cross-checked against the
#: registry rather than trusted blindly: a disagreement between what the
#: builder thinks the layout is and what the image was actually linked
#: for is exactly the kind of silent mismatch that produces firmware
#: which builds, flashes and then does not boot.
HEADER_SIZE_SYMBOL = "CONFIG_ROM_START_OFFSET"

#: The built application's Kconfig, next to its artifacts. Sysbuild's
#: output layout itself is :data:`mcuhome.compiler.workspace.IMAGE_OUTPUT_DIR`, so
#: this module states the one file it reads there and nothing else.
KCONFIG_FILE = ".config"


def kconfig_path(build_dir: Path, app_image: str) -> Path:
    """Where the built application left the ``.config`` this module reads."""
    return workspace.image_output(build_dir, app_image) / KCONFIG_FILE


def read_kconfig(path: Path) -> dict[str, str]:
    """A Zephyr ``.config`` as ``symbol -> value``, quotes stripped.

    Deliberately not a Kconfig parser: the file is already the *result*
    of one, one assignment per line, and the builder reads two symbols
    out of it.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        if not line.startswith("CONFIG_") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"')
    return values


def _as_int(text: str, fallback: int) -> int:
    try:
        return int(text, 0)
    except (TypeError, ValueError):
        return fallback


def signing_parameters(
    scheme: registry.UpdateSchemeDef, *, kconfig: dict[str, str] | None = None
) -> SigningParameters:
    """The arguments the inline build signs with, from board data.

    Three of the four are the registry's: the header offset and the write
    alignment are properties of the part, and the slot size is the
    partition table ADR 0015 decision 2 makes per-board data — the same
    table the builder rendered into the overlay this image was linked
    against, which is why it can state them without asking the build.

    *kconfig* is the built application's ``.config`` when there is one.
    It carries imgtool's ``--version``, which the builder does not decide,
    and it is where the header offset is cross-checked: a linker that
    reserved a different offset than the layout assumes produces firmware
    that builds and does not boot, and that is worth a refusal rather than
    a manifest stating something untrue.
    """
    values = kconfig or {}
    header_size = _as_int(values.get(HEADER_SIZE_SYMBOL, ""), scheme.header_size)
    if header_size != scheme.header_size:
        raise BuildError(
            f"The application was linked with a {header_size}-byte MCUboot header "
            f"offset, but this board's layout says {scheme.header_size}.",
            hint=(
                f"{HEADER_SIZE_SYMBOL} and the board's header_size in "
                "mcuhome/model/registry.py have to agree — an image signed against the "
                "wrong offset boots nowhere. This is a builder bug worth reporting."
            ),
        )
    return SigningParameters(
        header_size=scheme.header_size,
        align=scheme.write_block_size,
        slot_size=scheme.imgtool_slot.size,
        version=values.get(SIGN_VERSION_SYMBOL) or SIGN_VERSION_DEFAULT,
    )


def _signing_block(
    *,
    out_dir: Path,
    build_dir: Path,
    app_image: str,
    scheme: registry.UpdateSchemeDef,
    signed_by_the_build: bool,
) -> SigningBlock:
    output = workspace.image_output(build_dir, app_image)
    relative = (output.resolve().relative_to(out_dir.resolve())).as_posix()
    return SigningBlock(
        image=app_image,
        signed_by_the_build=signed_by_the_build,
        signature_type=registry.SIGNATURE_TYPE,
        parameters=signing_parameters(
            scheme, kconfig=read_kconfig(kconfig_path(build_dir, app_image))
        ),
        inputs={
            "bin": f"{relative}/{workspace.BIN_ARTIFACT}",
            "hex": f"{relative}/{workspace.HEX_ARTIFACT}",
        },
        outputs={
            "bin": f"{relative}/zephyr.signed.bin",
            "hex": f"{relative}/zephyr.signed.hex",
        },
    )


def build_manifest(
    model: DeviceModel,
    *,
    out_dir: Path,
    build_dir: Path,
    app_image: str,
    images: list[workspace.ImageArtifacts],
    snippets: tuple[str, ...] = (),
    bootloader_snippets: tuple[str, ...] = (),
    jobs: int,
    signed_by_the_build: bool,
    merged: Path | None = None,
    ota_image: ota.OtaImage | None = None,
) -> BuildManifest:
    """Describe what came out of stage 5, hashing every file it names."""
    board = registry.BOARDS.get(model.device.board)
    scheme = None if board is None else board.update_scheme
    return BuildManifest(
        device=model.device.name,
        friendly_name=model.device.friendly_name,
        board=model.device.board,
        version=model.device.version,
        model_version=model.model_version,
        builder_version=__version__,
        snippets=tuple(snippets),
        bootloader_snippets=tuple(bootloader_snippets),
        jobs=jobs,
        images=tuple(
            ImageEntry(
                name=image.name,
                role=image.role,
                flash_bytes=image.flash_bytes,
                files=tuple(FileEntry.measure(path, out_dir=out_dir) for path in image.files),
            )
            for image in images
        ),
        signing=(
            None
            if scheme is None
            else _signing_block(
                out_dir=out_dir,
                build_dir=build_dir,
                app_image=app_image,
                scheme=scheme,
                signed_by_the_build=signed_by_the_build,
            )
        ),
        merged=None if merged is None else FileEntry.measure(merged, out_dir=out_dir),
        ota=(ota_parameters(model) if ota_image is None else ota_entry(ota_image, out_dir=out_dir)),
    )


def write_manifest(manifest: BuildManifest, *, out_dir: Path) -> Path:
    """Write the manifest into the build directory and return its path."""
    path = out_dir / MANIFEST_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest.to_json(), encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"The build manifest {path} cannot be written: {error.strerror}.",
            hint="pick a writable location with --build-dir",
        ) from error
    return path
